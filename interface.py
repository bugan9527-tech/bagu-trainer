# -*- coding: utf-8 -*-
"""商品查询接口示例：Redis 缓存层 + 防缓存穿透。

缓存穿透：请求一个根本不存在的商品 ID（非法 ID、已下架商品），
缓存永远不命中，每次请求都打到 DB，恶意刷量可压垮数据库。

本文件的三道防护：
  1. 入口参数校验：非法 ID（非数字、<=0）直接拒绝，不进缓存也不进 DB；
  2. 空值缓存：DB 查不到时，把"查不到"这个事实以短 TTL 写入缓存，
     后续相同请求命中空值标记直接返回，不再穿透 DB。
     （高吞吐场景可再加布隆过滤器，在缓存前拦截；空值缓存是成本最低的方案）
  3. SQL 预编译（参数化查询）：DB 查询一律用 ? 占位符 + 参数绑定，
     SQL 结构与数据彻底分离，用户输入永远只作为"值"绑定，
     不会被当作 SQL 代码解析，从根上杜绝注入。
  4. 条数上限 + LRU 淘汰：缓存条目数超过 MAX_CACHE_ITEMS 时，
     按"最久未访问"淘汰，防止商品量巨大时缓存无限累积撑爆 Redis 内存。

运行依赖：pip install redis（需本机或远端有 Redis 服务）；sqlite3 为标准库自带
"""
import json
import sqlite3

import redis

# ---- 配置（生产环境应从配置中心/环境变量读取）----
REDIS_HOST = "127.0.0.1"
REDIS_PORT = 6379
REDIS_DB = 0
CACHE_TTL = 600   # 正常商品缓存 10 分钟
NULL_TTL = 60     # 空值标记只缓存 60 秒：足够挡穿透，又不至于误伤新上架商品
NULL_MARKER = "__NULL__"  # 空值哨兵，与正常商品的 JSON 字符串区分
MAX_CACHE_ITEMS = 1000    # 缓存条数上限（含空值标记），超出后按 LRU 淘汰最久未访问的 key
LRU_ZSET = "product:lru"      # LRU 记账 ZSET：member=缓存 key，score=最近访问序号（越大越新）
LRU_COUNTER = "product:lru:seq"  # 全局自增 "访问序号" 计数器，保证 score 唯一且可比

_client = None


def _redis() -> redis.Redis:
    """懒加载 Redis 连接（redis-py 连接本身是惰性的，命令执行时才真正建连）"""
    global _client
    if _client is None:
        _client = redis.Redis(
            host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True
        )
    return _client


# ---- 模拟数据库：唯一的"慢"数据源（SQLite 演示层） ----
_SEED = [
    (1001, "Redis 实战", 59.9, 12),
    (1002, "八股训练器 Pro", 199.0, 3),
]

_conn = None


def _db() -> sqlite3.Connection:
    """懒加载 SQLite 连接，首次调用时建表并灌入种子数据"""
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(":memory:")
        _conn.execute(
            "CREATE TABLE products ("
            "id INTEGER PRIMARY KEY, name TEXT, price REAL, stock INTEGER)"
        )
        _conn.executemany(
            "INSERT INTO products (id, name, price, stock) VALUES (?, ?, ?, ?)",
            _SEED,
        )
    return _conn


def _query_db(product_id):
    """参数化查询（预编译）：SQL 结构与数据分离，查不到返回 None。

    驱动先把带 ? 占位符的 SQL 预编译成执行计划，再把 product_id 作为
    "值"绑定进去。即使传入 "1 OR 1=1" 之类的注入片段，它也只是一段
    字符串数据，绝不会被二次解析成 SQL 代码。

    反例（切勿在生产使用）：
        q = f"SELECT * FROM products WHERE id = {product_id}"  # 拼字符串 = 注入口
    """
    row = _db().execute(
        "SELECT id, name, price, stock FROM products WHERE id = ?", (product_id,)
    ).fetchone()
    if row is None:
        return None
    return dict(zip(("id", "name", "price", "stock"), row))


def _cache_key(product_id: int) -> str:
    return f"product:{product_id}"


def _lru_touch(key: str) -> None:
    """登记/刷新 key 为"最近访问"。

    Redis 没有现成的 LRU 记账结构，这里用 ZSET 实现：
    score 取全局自增序号（INCR），序号越大代表访问越新；
    ZSET 按 score 升序排列，队头就是最久未访问的 key。
    用自增序号而不是时间戳，是因为同一毫秒内并发写时间戳会重复、
    score 相同无法稳定比较，序号则天然唯一。
    """
    r = _redis()
    r.zadd(LRU_ZSET, {key: r.incr(LRU_COUNTER)})


def _lru_evict() -> None:
    """缓存条数超上限时，从 LRU 队头淘汰最久未访问的 key。

    只在回填新 key 时调用，ZCARD 是 O(1) 判断，几乎无损。
    小瑕疵：ZSET 记账条目不过期。若某 key 到 TTL 自然过期后从未被再次
    查询，它会暂时占一个名额，直到某次淘汰被顺带清除——只会让淘汰
    略偏早，不会误删仍有效的 key，可接受。
    """
    r = _redis()
    overflow = r.zcard(LRU_ZSET) - MAX_CACHE_ITEMS
    if overflow <= 0:
        return
    victims = r.zrange(LRU_ZSET, 0, overflow - 1)  # 队头 overflow 个 = 最久未访问
    if victims:
        r.delete(*victims)
        r.zrem(LRU_ZSET, *victims)


def query_product(product_id) -> dict | None:
    """商品查询接口：缓存命中直接返回，未命中查 DB 并回填缓存。

    返回商品 dict；商品不存在或参数非法返回 None。
    """
    # 0) 参数校验（防穿透第一道闸：非法 ID 不进 DB）
    try:
        pid = int(product_id)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None

    key = _cache_key(pid)

    # 1) 读缓存
    try:
        hit = _redis().get(key)
    except redis.RedisError:
        hit = None  # Redis 故障降级：直接查 DB，不让缓存故障拖垮接口

    if hit is not None:
        try:
            _lru_touch(key)  # 命中也要刷新访问时间，LRU 才知道"最近谁被用过"
        except redis.RedisError:
            pass
        if hit == NULL_MARKER:
            return None  # 之前查过且确认不存在（防穿透第二道闸生效）
        return json.loads(hit)

    # 2) 缓存未命中 -> 查 DB
    product = _query_db(pid)

    # 3) 回填缓存：查到写正常 TTL；查不到写空值标记 + 短 TTL
    try:
        if product is not None:
            _redis().set(key, json.dumps(product, ensure_ascii=False), ex=CACHE_TTL)
        else:
            _redis().set(key, NULL_MARKER, ex=NULL_TTL)
        _lru_touch(key)  # 新 key 登记为"最新访问"，同时让记账条数 +1
        _lru_evict()     # 条数超过上限则按 LRU 淘汰最久未访问的 key
    except redis.RedisError:
        pass  # 写缓存失败不影响本次查询结果

    return product


if __name__ == "__main__":
    # 演示：1001 正常命中；9999 不存在 -> 第一次穿透后写空值，第二次直接挡掉；
    # "abc"、-5 被参数校验直接拒绝（不会产生 DB 查询）。
    for pid in (1001, 9999, "abc", -5, 9999):
        print(f"query_product({pid!r}) -> {query_product(pid)}")

    # 第三道闸单测：绕过参数校验，把注入串直接打到 SQL 层。
    # 预编译 + 参数绑定后，注入串只被当作"值"参与比较，查不到任何行。
    for evil in ("1 OR 1=1", "1001' OR '1'='1", "1001'; DROP TABLE products; --"):
        got = _query_db(evil)
        print(f"_query_db({evil!r}) -> {got}  (注入被拦截)"
              if got is None else f"!!! 注入成功: {got}")