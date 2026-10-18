"""OpenRouter 这条决策模型通路（D-120）：契约、键名对齐、失败取向、以及会骗人的报错。

为什么单独一个文件：**这条通路的契约是我们自己写的**（原生那条靠服务端给结构化答案，
这里靠提示词 + `_check_*` 逐条验）。于是最危险的失效方式不是报错，而是"形状变了但看着正常"：

* `noul` 从概率变成布尔 —— 仓库踩过同型的坑，每个问题都读成"否"还把责任推给模型；
* `score` 变成字符串（模型回"高"）—— `int(float('高'))` 抛的是 `ValueError`，
  调用方的 `except JevError` **接不住**，整天日报会挂；
* token 键名对不上（OpenRouter 是 `prompt_tokens`，原生是 `input_tokens`）——
  成本统计静默变 0，而没人会去核对一个 0；
* 空正文（推理 token 吃光预算）—— 看着像"模型坏了"，其实是预算给少了。

四条都逐条钉住。HTTP 全部打桩，这个文件不联网。
"""
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
import urllib.error
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location('jev_client_or', SCRIPTS / 'jev_client.py')
jev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jev)

import _utils  # noqa: E402  (scripts/ 已在 sys.path 上)

TOPICS = ['AI', '学术', '新闻', '文学']
SECRET = 'sk-or-v1-THIS-MUST-NEVER-APPEAR'


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload, ensure_ascii=False).encode('utf-8')

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def chat_body(content, cost=0.0001, model='x-ai/grok-4.7'):
    return {
        'model': model,
        'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant',
                                                                    'content': content}}],
        'usage': {'prompt_tokens': 3100, 'completion_tokens': 160, 'total_tokens': 3260,
                  'cost': cost},
    }


def contract(topic='AI', score=1, noul=0.62):
    return json.dumps({
        'topic': {'probabilities': {t: (0.7 if t == topic else 0.1) for t in TOPICS},
                  'choice': topic},
        'relevance': {'score': score},
        'worth_including': {'noul': noul},
    }, ensure_ascii=False)


def questions():
    return jev.build_questions(TOPICS)


class PromptTests(unittest.TestCase):
    def test_候选与判据都进提示词(self):
        """criteria 的**文本**是模型的判断依据，漏了它模型就是在没标准的情况下猜。"""
        prompt = jev.build_openrouter_prompt('材料正文', questions())
        for topic in TOPICS:
            self.assertIn('"%s"' % topic, prompt)
        self.assertIn(jev.TOPIC_CRITERIA['AI'][:12], prompt)
        self.assertIn('材料正文', prompt)
        for key in ('"topic"', '"relevance"', '"worth_including"'):
            self.assertIn(key, prompt)

    def test_noul_被明确要求成概率而不是布尔(self):
        """提示词里少了这句，模型给 true/false 就会被 `_check_noul` 挡下来 —— 那是白跑一次。"""
        prompt = jev.build_openrouter_prompt('材料', questions())
        self.assertIn('true/false', prompt)
        self.assertIn('概率', prompt)

    def test_score_题的刻度写全(self):
        prompt = jev.build_openrouter_prompt('材料', questions())
        for level in jev.RELEVANCE_LEVELS:
            self.assertIn(level[:6], prompt)


class ParseTests(unittest.TestCase):
    def test_从代码块和废话里取出对象(self):
        parsed = jev._parse_json_object('好的，结果如下：\n```json\n{"a": 1}\n```\n完。')
        self.assertEqual(parsed, {'a': 1})

    def test_空正文点名预算(self):
        """空 content 最常见的原因是推理 token 吃光预算 —— 报错要说出来，别让人以为模型坏了。"""
        with self.assertRaises(jev.JevError) as ctx:
            jev._parse_json_object('', 'chat 响应')
        self.assertIn('OPENROUTER_MAX_TOKENS', str(ctx.exception))

    def test_不是_JSON_要报错而不是猜(self):
        with self.assertRaises(jev.JevError):
            jev._parse_json_object('这篇文章属于 AI 类。')

    def test_顶层不是对象要报错(self):
        with self.assertRaises(jev.JevError):
            jev._parse_json_object('[1, 2, 3]')


class ValidatorTests(unittest.TestCase):
    def test_score_必须是数字(self):
        question = {'type': 'score'}
        jev._check_score('relevance', question, {'score': 1.8})       # 通过
        for bad in ('高', None, True):
            with self.subTest(bad=bad):
                with self.assertRaises(jev.JevError):
                    jev._check_score('relevance', question, {'score': bad})

    def test_noul_必须是_0_到_1_的概率(self):
        question = {'type': 'noul'}
        jev._check_noul('w', question, {'noul': 0.9})
        for bad in (True, False, '是', 1.2, -0.1):
            with self.subTest(bad=bad):
                with self.assertRaises(jev.JevError):
                    jev._check_noul('w', question, {'noul': bad})

    def test_类型不匹配时这些校验不误伤(self):
        """`choice` 题不该被 score/noul 的校验碰到（它们是并列的三条路，不是叠加的）。"""
        jev._check_score('topic', {'type': 'choice'}, {'probabilities': {}, 'choice': 'AI'})
        jev._check_noul('topic', {'type': 'choice'}, {'probabilities': {}, 'choice': 'AI'})


class DecideTests(unittest.TestCase):
    def client(self):
        return jev.ChatJevClient(SECRET, timeout=5)

    def test_正常一轮_契约与键名都对上(self):
        with patch.object(jev.urllib.request, 'urlopen',
                          return_value=FakeResponse(chat_body(contract(), cost=0.0139))):
            answers, usage = self.client().decide('材料', questions())
        self.assertEqual(answers['topic']['choice'], 'AI')
        self.assertEqual(usage['input_tokens'], 3100)      # 从 prompt_tokens 对齐过来
        self.assertEqual(usage['output_tokens'], 160)
        self.assertEqual(usage['cost'], 0.0139)
        self.assertEqual(usage['model'], 'x-ai/grok-4.7')  # 路由到谁要落盘
        self.assertEqual(self.client().last_model, None)   # 不同实例互不影响
        client = self.client()
        with patch.object(jev.urllib.request, 'urlopen',
                          return_value=FakeResponse(chat_body(contract()))):
            client.decide('材料', questions())
        self.assertEqual(client.last_model, 'x-ai/grok-4.7')

    def test_请求形状_端点头与参数(self):
        seen = {}

        def fake_urlopen(request, timeout=None):
            seen['url'] = request.full_url
            seen['auth'] = request.headers.get('Authorization')
            seen['body'] = json.loads(request.data.decode('utf-8'))
            return FakeResponse(chat_body(contract()))

        with patch.object(jev.urllib.request, 'urlopen', fake_urlopen):
            self.client().decide('材料', questions())
        self.assertEqual(seen['url'], jev.OPENROUTER_ENDPOINT)
        self.assertEqual(seen['auth'], 'Bearer ' + SECRET)
        self.assertEqual(seen['body']['model'], jev.OPENROUTER_DEFAULT_MODEL)
        self.assertEqual(seen['body']['temperature'], 0)
        self.assertEqual(seen['body']['max_tokens'], jev.OPENROUTER_MAX_TOKENS)
        self.assertIn('材料', seen['body']['messages'][0]['content'])

    def test_每条契约违约都当场炸(self):
        cases = {
            '概率和不等于 1': {'topic': {'probabilities': {t: 0.1 for t in TOPICS}, 'choice': 'AI'},
                          'relevance': {'score': 1}, 'worth_including': {'noul': 0.5}},
            '键集与 criteria 不一致': {'topic': {'probabilities': {'AI': 1.0}, 'choice': 'AI'},
                                'relevance': {'score': 1}, 'worth_including': {'noul': 0.5}},
            'choice 不是 argmax': {'topic': {'probabilities': {t: (0.7 if t == '文学' else 0.1)
                                                               for t in TOPICS}, 'choice': 'AI'},
                                 'relevance': {'score': 1}, 'worth_including': {'noul': 0.5}},
            'noul 是布尔': {'topic': {'probabilities': {t: (0.7 if t == 'AI' else 0.1) for t in TOPICS},
                                  'choice': 'AI'}, 'relevance': {'score': 1},
                            'worth_including': {'noul': True}},
            'score 是字符串': {'topic': {'probabilities': {t: (0.7 if t == 'AI' else 0.1) for t in TOPICS},
                                    'choice': 'AI'}, 'relevance': {'score': '高'},
                              'worth_including': {'noul': 0.5}},
        }
        for name, payload in cases.items():
            with self.subTest(case=name):
                body = chat_body(json.dumps(payload, ensure_ascii=False))
                with patch.object(jev.urllib.request, 'urlopen', return_value=FakeResponse(body)):
                    with self.assertRaises(jev.JevError):
                        self.client().decide('材料', questions())

    def test_少答一题也炸(self):
        payload = json.loads(contract())
        payload.pop('worth_including')
        with patch.object(jev.urllib.request, 'urlopen',
                          return_value=FakeResponse(chat_body(json.dumps(payload)))):
            with self.assertRaises(jev.JevError):
                self.client().decide('材料', questions())

    def test_空正文给出可行动的报错(self):
        with patch.object(jev.urllib.request, 'urlopen',
                          return_value=FakeResponse(chat_body(''))):
            with self.assertRaises(jev.JevError) as ctx:
                self.client().decide('材料', questions())
        self.assertIn('预算', str(ctx.exception))

    def test_决定性端到端_继承来的路径也能用(self):
        with patch.object(jev.urllib.request, 'urlopen',
                          return_value=FakeResponse(chat_body(contract(topic='学术', score=1.6,
                                                                       noul=0.31)))):
            result = self.client().decide_article('标题', '正文', TOPICS)
        self.assertEqual(result['topic'], '学术')
        self.assertEqual(result['relevance'], '高')
        self.assertEqual(result['relevanceScore'], 1.6)
        self.assertEqual(result['includeScore'], 0.31)


class ErrorMappingTests(unittest.TestCase):
    def raise_http(self, code, payload):
        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, code, 'err', None,
                                         io.BytesIO(json.dumps(payload).encode('utf-8')))
        return patch.object(jev.urllib.request, 'urlopen', fake_urlopen)

    def test_403_地区封锁要说清是地区不是鉴权(self):
        """混在一起会让人去换 key，而这件事换 key 没用、换模型才有用。"""
        payload = {'error': {'message': 'This model is not available in your region.'}}
        with self.raise_http(403, payload):
            with self.assertRaises(jev.JevError) as ctx:
                jev.ChatJevClient(SECRET).decide('材料', questions())
        self.assertIn('地区', str(ctx.exception))

    def test_402_要说成额度不足(self):
        payload = {'error': {'message': 'This request requires more credits, or fewer max_tokens.'}}
        with self.raise_http(402, payload):
            with self.assertRaises(jev.JevError) as ctx:
                jev.ChatJevClient(SECRET).decide('材料', questions())
        self.assertIn('额度', str(ctx.exception))

    def test_401_才是鉴权(self):
        with self.raise_http(401, {'error': {'message': 'No auth credentials found'}}):
            with self.assertRaises(jev.JevError) as ctx:
                jev.ChatJevClient(SECRET).decide('材料', questions())
        self.assertIn('鉴权', str(ctx.exception))

    def test_报错里不许出现_key(self):
        for code in (401, 402, 403, 500):
            with self.subTest(code=code):
                with self.raise_http(code, {'error': {'message': 'boom'}}):
                    try:
                        jev.ChatJevClient(SECRET).decide('材料', questions())
                    except jev.JevError as error:
                        self.assertNotIn(SECRET, str(error))


class FactoryTests(unittest.TestCase):
    """`create_client` 的分派：默认必须是 native —— 这个开关不该替所有人改行为。"""

    def make(self, provider='native', typesafe='tk', or_key='', or_model='', engine='deepseek'):
        return patch.multiple(_utils,
                              get_judge_provider=lambda config=None: provider,
                              get_openrouter_key=lambda config=None: or_key,
                              get_openrouter_model=lambda config=None: or_model,
                              get_typesafe_key=lambda config=None: typesafe,
                              get_ai_engine=lambda config=None: engine)

    def test_默认走_native(self):
        with self.make(provider='', typesafe='tk'), \
                patch.object(jev.urllib.request, 'urlopen', return_value=FakeResponse({})):
            client = jev.create_client(config={})
        self.assertIsInstance(client, jev.JevClient)
        self.assertNotIsInstance(client, jev.ChatJevClient)

    def test_openrouter_取配置里的模型(self):
        with self.make(provider='openrouter', or_key='ork', or_model='typesafe/jev-router'):
            client = jev.create_client(config={})
        self.assertIsInstance(client, jev.ChatJevClient)
        self.assertEqual(client.model, 'typesafe/jev-router')
        self.assertEqual(client.api_key, 'ork')

    def test_显式模型覆盖配置(self):
        with self.make(provider='openrouter', or_key='ork', or_model='a/b'):
            self.assertEqual(jev.create_client(model='x/y', config={}).model, 'x/y')

    def test_openrouter_没_key_时不静默(self):
        """静默返回 None 的表现是"判断又开始退化了"，而配错的人不会知道原因。"""
        stderr = io.StringIO()
        with self.make(provider='openrouter', or_key=''), patch.object(sys, 'stderr', stderr):
            client = jev.create_client(config={})
        self.assertIsNone(client)
        self.assertIn('openrouterApiKey', stderr.getvalue())

    def test_不认识的取值退回_native(self):
        self.assertIsInstance(jev.create_client(config={}, api_key='tk'),
                              jev.JevClient)
        with self.make(provider='opernrouter', typesafe='tk'):        # 拼错的取值
            self.assertIsInstance(jev.create_client(config={}), jev.JevClient)

    def test_没有_key_就是_None(self):
        with self.make(provider='native', typesafe=''):
            self.assertIsNone(jev.create_client(config={}))


class ChatProviderTests(unittest.TestCase):
    """`judgeProvider=chat`：**中国地区能通**的那条路（本机实测 deepseek-chat 1.0s、契约通过）。

    为什么它值得单独钉：这条路不需要新厂商、新 key —— 它复用配置里已有的对话模型
    （`aiBaseUrl` 中转站 / `aiModel` / `deepseekApiKey`）。复用错了端点不会报错，
    只会让判断质量悄悄变了，所以端点与模型的取值逐个钉住。
    """

    def make(self, or_key='', typesafe='tk', base=None, chat_model=None, deepseek='dk'):
        return patch.multiple(_utils,
                              get_judge_provider=lambda config=None: 'chat',
                              get_openrouter_key=lambda config=None: or_key,
                              get_openrouter_model=lambda config=None: '',
                              get_chat_endpoint=lambda config=None: (base, chat_model),
                              get_api_key=lambda config=None: deepseek,
                              get_typesafe_key=lambda config=None: typesafe)

    def test_没配中转站就直连_DeepSeek(self):
        with self.make():
            client = jev.create_client(config={})
        self.assertIsInstance(client, jev.ChatJevClient)
        self.assertEqual(client.base_url, jev.DEEPSEEK_BASE)
        self.assertEqual(client.model, 'deepseek-chat')
        self.assertEqual(client.provider, 'chat')
        self.assertEqual(client.api_key, 'dk')

    def test_配了中转站就用中转站(self):
        """`aiBaseUrl` 那条路必须被尊重 —— 忽略它 = 发去了错的端点。"""
        with self.make(base='https://relay.example.com/v1', chat_model='glm-4.6'):
            client = jev.create_client(config={})
        self.assertEqual(client.base_url, 'https://relay.example.com/v1')
        self.assertEqual(client.model, 'glm-4.6')

    def test_显式模型覆盖配置(self):
        with self.make(base=None, chat_model=None):
            self.assertEqual(jev.create_client(model='x/y', config={}).model, 'x/y')

    def test_没有对话模型_key_时不静默(self):
        stderr = io.StringIO()
        with self.make(deepseek=''), patch.object(sys, 'stderr', stderr):
            self.assertIsNone(jev.create_client(config={}))
        self.assertIn('deepseekApiKey', stderr.getvalue())

    def test_不带归属头与_OpenRouter_端点(self):
        """国内端点不该收到 OpenRouter 的归属头，也不该往 OpenRouter 发。"""
        with self.make():
            client = jev.create_client(config={})
        self.assertEqual(client.extra_headers, {})
        self.assertNotIn('openrouter', client._endpoint())

    def test_国内端点的_models_不报错(self):
        """国内端点没有同形的清单接口：返回空列表，而不是抛错把调用方带下去。"""
        with self.make():
            self.assertEqual(jev.create_client(config={}).models(), [])


class LocalEngineTests(unittest.TestCase):
    """`judgeProvider=chat` 必须尊重 `aiEngine`（D-124）。

    为什么这是**隐私**而不是偏好：`chat` 通路的语义是"用配置里的对话模型"，而
    `aiEngine=local` 是用户明确选了不出境。2026-10-18 之前那条路无条件读 `deepseekApiKey` ——
    一开 `judgeProvider=chat`，文章正文就发去了 DeepSeek，而界面上什么都没变。
    """

    class StubEngine:
        base_url = 'http://localhost:11434/v1'
        model = 'qwen2.5:7b'

        def __init__(self, content):
            self.content = content

        def chat(self, prompt, max_tokens=2000):
            return self.content

    def make(self, engine, detected=(True, None, ''), deepseek='dk-secret'):
        ok, local_engine, desc = detected
        if local_engine is None and ok:
            local_engine = self.StubEngine(contract())
        return patch.multiple(_utils,
                              get_judge_provider=lambda config=None: 'chat',
                              get_ai_engine=lambda config=None: engine,
                              get_api_key=lambda config=None: deepseek,
                              get_chat_endpoint=lambda config=None: (None, None),
                              get_openrouter_key=lambda config=None: '',
                              get_openrouter_model=lambda config=None: '',
                              get_typesafe_key=lambda config=None: 'tk',
                              detect_local_engine=lambda: (ok, local_engine, desc or '无可用'))

    def test_本地引擎时不碰云端_key(self):
        with self.make('local'):
            client = jev.create_client(config={})
        self.assertIsInstance(client, jev.ChatJevClient)
        self.assertEqual(client.provider, 'chat-local')
        self.assertEqual(client.api_key, 'local', '本地通路不该拿云端 key')
        self.assertEqual(client.base_url, 'http://localhost:11434/v1')
        self.assertEqual(client.model, 'qwen2.5:7b')
        self.assertIsNotNone(client.engine)

    def test_本地服务不可用时不退回云端(self):
        stderr = io.StringIO()
        with self.make('local', detected=(False, None, '无可用本地推理服务')), \
                patch.object(sys, 'stderr', stderr):
            client = jev.create_client(config={})
        self.assertIsNone(client, '本地不可用时宁可没有判断，也不许改用 DeepSeek')
        self.assertIn('不会退回云端', stderr.getvalue())

    def test_claude_引擎明确拒绝而不是换厂商(self):
        stderr = io.StringIO()
        with self.make('claude'), patch.object(sys, 'stderr', stderr):
            self.assertIsNone(jev.create_client(config={}))
        self.assertIn('openrouter', stderr.getvalue())

    def test_ollama_也按本地处理(self):
        with self.make('ollama'):
            self.assertEqual(jev.create_client(config={}).provider, 'chat-local')

    def test_默认_deepseek_行为不变(self):
        with self.make('deepseek'):
            client = jev.create_client(config={})
        self.assertEqual(client.provider, 'chat')
        self.assertEqual(client.api_key, 'dk-secret')

    def test_engine_路径也能满足契约(self):
        """本地引擎只回字符串（没有 usage），但契约校验与 `last_model` 必须照常。"""
        client = jev.ChatJevClient('local', model='local', engine=self.StubEngine(contract()))
        answers, usage = client.decide('材料', questions())
        self.assertEqual(answers['topic']['choice'], 'AI')
        self.assertEqual(usage['model'], 'qwen2.5:7b')
        self.assertEqual(client.last_model, 'qwen2.5:7b')

    def test_engine_返回垃圾也照样炸(self):
        client = jev.ChatJevClient('local', model='local', engine=self.StubEngine('这不是 JSON'))
        with self.assertRaises(jev.JevError):
            client.decide('材料', questions())


class ProviderResolutionTests(unittest.TestCase):
    """`_utils` 那三个读取函数本身：默认值、取值校验、解密失败的态度。"""

    def test_默认_native_且不认识的值也回_native(self):
        self.assertEqual(_utils.get_judge_provider({}), 'native')
        self.assertEqual(_utils.get_judge_provider({'judgeProvider': 'OPENROUTER'}), 'openrouter')
        self.assertEqual(_utils.get_judge_provider({'judgeProvider': 'CHAT'}), 'chat')
        self.assertEqual(_utils.get_judge_provider({'judgeProvider': '什么鬼'}), 'native')

    def test_读不出配置时不崩(self):
        with patch.object(_utils, 'load_config', side_effect=OSError('没有配置文件')):
            self.assertEqual(_utils.get_judge_provider(), 'native')
            self.assertEqual(_utils.get_openrouter_key(), '')
            self.assertEqual(_utils.get_openrouter_model(), '')


if __name__ == '__main__':
    unittest.main()
