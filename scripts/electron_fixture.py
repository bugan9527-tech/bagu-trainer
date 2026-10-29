"""Synthetic offline desktop acceptance profile; never imports developer data."""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def create(destination, model_url=None):
    destination = Path(destination).resolve()
    os.environ['BAGU_USER_DATA_DIR'] = str(destination)
    destination.mkdir(parents=True, exist_ok=False)
    project = destination / '示例代码项目'
    project.mkdir()
    (project / 'cache.py').write_text('def invalidate_cache(key):\n    return key\n', encoding='utf-8')
    (destination / 'config.json').write_text(json.dumps({
        'project_roots': [str(project)], 'session_ingest': False,
        'llm': {'base_url': model_url or 'http://127.0.0.1:9/v1', 'model': 'offline-acceptance', 'api_key': '', 'timeout_sec': 2},
    }), encoding='utf-8')
    data = destination / 'data'
    data.mkdir()
    questions = [{'id': f'fixture-{number}', 'knowledge_point': name, 'tags': ['Redis'],
                  'keywords': ['redis', '缓存'], 'question': f'{name}如何处理？', 'difficulty': 'medium'}
                 for number, name in enumerate(['缓存一致性', '缓存穿透'], 1)]
    (data / 'questions.json').write_text(json.dumps(questions, ensure_ascii=False), encoding='utf-8')
    from bagu_trainer.infrastructure.store import Store
    from bagu_trainer.domain.quizzes import grounding_receipt
    receipt = grounding_receipt(
        {'context_quote': '合成缓存测试', 'source_quote': '自编题库来源用于验证缓存一致性的学习流程。',
         'rationale': '自编夹具将缓存更新场景与知识点关联。'},
        {'context_relevant': True, 'source_supported': True, 'answer_unique': True,
         'no_answer_leak': True, 'correct_answer': 'A', 'reason': '自编验收夹具，答案与场景均可核对。'},
        '合成缓存测试', '自编题库来源用于验证缓存一致性的学习流程。', 'offline-acceptance')
    store = Store(data / 'trainer.db')
    try:
        for number, knowledge in enumerate(questions, 1):
            snapshot = {'kp_id': knowledge['id'], 'kp_name': knowledge['knowledge_point'],
                'question': '你正在为订单服务维护 Redis 缓存。数据库更新成功后，旧缓存仍被读取，导致页面展示过期信息。下面哪种处理方式更适合改善该场景的一致性？\n\n```python\ndef update_order(order):\n    database.save(order)\n    cache.delete(order.id)\n```',
                'options': {'A': '先更新数据库，再删除对应缓存，并处理删除失败的重试。',
                    'B': '只更新缓存，不再更新数据库。', 'C': '永久保留缓存，让客户端自行识别过期数据。',
                    'D': '每次写入都清空全部用户的缓存，并禁用错误重试。'},
                'answer': 'A', 'analysis': '更新数据库后删除缓存，可以让后续读取重新加载数据。还需要处理删除失败，并结合业务要求考虑并发与一致性。\n\n这是**自编验收题**，未调用真实模型。',
                'difficulty': 'medium', 'generated_by': 'llm', 'grounding': receipt}
            store.create_quiz(f'electron-fixture-{number}', knowledge['id'], snapshot, 'ready', str(project),
                              '自编离线验收夹具', time.time() + number, time.time() + 86400)
    finally:
        store.close()
    if model_url:
        # Build/cache an entirely authored source archive. The running backend
        # can then exercise bank.build and reload using its ordinary offline cache.
        import io
        import zipfile
        from bagu_trainer.infrastructure.bank_build.service import BankBuildService, load_sources
        source = next(iter(load_sources().values()))
        excerpt = ('数据库更新成功后应删除 Redis 缓存，让后续读取重新加载数据库中的最新数据。'
                   '缓存删除失败需要显式处理，结合业务要求控制并发更新与过期时间。'
                   'redis_cache_generation_marker 表示本次合成订单更新，所有示例数据均为自编。')
        bundle = io.BytesIO()
        with zipfile.ZipFile(bundle, 'w') as archive:
            archive.writestr('synthetic/README.md', '# Redis 缓存一致性如何处理？\n\n' + excerpt
                + '\n\n# Java 空指针异常如何排查？\n\nJava 程序在解引用空对象时会抛出空指针异常，'
                  '需要结合异常堆栈定位调用位置，检查对象初始化、参数校验和返回值，避免继续使用空引用。'
                + '\n\n# MySQL 数据库索引结构如何选择？\n\n数据库索引用额外的数据结构记录键值与数据位置，'
                  '查询利用索引缩小扫描范围，插入和更新会增加维护成本，需要结合查询条件选择合适的索引。')
        class Response(io.BytesIO):
            headers = {}
        service = BankBuildService(destination, opener=lambda *_args, **_kwargs: Response(bundle.getvalue()))
        service.build([source.repository])
        (data / 'sources.json').write_text(json.dumps({'sources': [{
            'repository': source.repository, 'directory': source.directory, 'commit': source.commit}]}), encoding='utf-8')


if __name__ == '__main__':
    create(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
