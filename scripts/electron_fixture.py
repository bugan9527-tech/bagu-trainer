"""Synthetic offline desktop acceptance profile; never imports developer data."""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def create(destination):
    destination = Path(destination).resolve()
    os.environ['BAGU_USER_DATA_DIR'] = str(destination)
    destination.mkdir(parents=True, exist_ok=False)
    project = destination / '示例代码项目'
    project.mkdir()
    (project / 'cache.py').write_text('def invalidate_cache(key):\n    return key\n', encoding='utf-8')
    (destination / 'config.json').write_text(json.dumps({
        'project_roots': [str(project)], 'session_ingest': False,
        'llm': {'base_url': 'http://127.0.0.1:9/v1', 'model': 'offline-acceptance', 'api_key': '', 'timeout_sec': 2},
    }), encoding='utf-8')
    data = destination / 'data'
    data.mkdir()
    questions = [{'id': f'fixture-{number}', 'knowledge_point': name, 'tags': ['Redis'],
                  'keywords': ['redis', '缓存'], 'question': f'{name}如何处理？', 'difficulty': 'medium'}
                 for number, name in enumerate(['缓存一致性', '缓存穿透'], 1)]
    (data / 'questions.json').write_text(json.dumps(questions, ensure_ascii=False), encoding='utf-8')
    from bagu_trainer.store import Store
    from bagu_trainer.quiz_validation import grounding_receipt
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


if __name__ == '__main__':
    create(sys.argv[1])
