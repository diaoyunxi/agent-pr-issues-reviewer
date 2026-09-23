"""不需要网络与模型的单元测试：配置加载、URL 脱敏、环境清洗、bash 工具。

跑法：`python agent/tests/test_units.py`（不依赖 pytest，CI 里也能直接跑）。
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from config import ConfigError, load_agent_config  # noqa: E402
from repo import RepoError, _authenticated_url, clone_repo, mask_url, sanitize_env  # noqa: E402
from task_context import build_context, build_prompt  # noqa: E402
from tools import ShellContext, run_bash  # noqa: E402

PASSED = []


def case(name):
    def wrapper(fn):
        fn()
        PASSED.append(name)
        return fn

    return wrapper


@case('mask_url 去掉令牌')
def _():
    masked = mask_url('https://x-access-token:ghp_secret@github.com/o/r.git')
    assert masked == 'https://***@github.com/o/r.git', masked
    assert 'ghp_secret' not in masked


@case('_authenticated_url 按平台拼用户名')
def _():
    gh = _authenticated_url('https://github.com/o/r.git', 'tok', 'github')
    assert gh == 'https://x-access-token:tok@github.com/o/r.git', gh
    gi = _authenticated_url('https://gitee.com/o/r.git', 'tok', 'gitee')
    assert gi == 'https://oauth2:tok@gitee.com/o/r.git', gi
    # 已经有凭据 / 非 https 的不改写
    assert _authenticated_url(gh, 'other', 'github') == gh
    assert _authenticated_url('/tmp/local/repo', 'tok', 'github') == '/tmp/local/repo'


@case('sanitize_env 剔除密钥且不改动原 dict')
def _():
    original = {'AI_API_KEY': 'k', 'GITHUB_TOKEN': 't', 'PATH': '/bin', 'FOO': 'bar'}
    env = sanitize_env(original, workspace=Path('/tmp/ws'))
    assert 'AI_API_KEY' not in env and 'GITHUB_TOKEN' not in env
    assert env['FOO'] == 'bar' and env['PATH'] == '/bin'
    assert env['HOME'] == '/tmp/ws' and env['PWD'] == '/tmp/ws'
    assert original['AI_API_KEY'] == 'k', '不得改动传入的 env'


@case('run_bash 在指定目录执行并回传退出码')
def _():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = ShellContext(workdir=tmp, timeout=10, env=sanitize_env({}, Path(tmp)))
        assert run_bash(ctx, 'pwd').strip().endswith(Path(tmp).name)
        assert run_bash(ctx, 'echo out; echo err >&2').startswith('out')
        failed = run_bash(ctx, 'exit 3')
        assert failed.startswith('[exit 3]'), failed
        assert run_bash(ctx, '') == '命令为空'


@case('run_bash 超时不会挂掉整个进程')
def _():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = ShellContext(workdir=tmp, timeout=1, env=sanitize_env({}, Path(tmp)))
        assert '超时' in run_bash(ctx, 'sleep 5')


@case('run_bash 输出截断')
def _():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = ShellContext(workdir=tmp, timeout=10, max_output_chars=50, env=sanitize_env({}, Path(tmp)))
        out = run_bash(ctx, "printf 'x%.0s' {1..200}")
        assert len(out) < 200 and '截断' in out, out


@case('配置加载：正常 + prompt 解析')
def _():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / 'agents').mkdir()
        (root / 'agents' / 'prompt.txt').write_text('你是一个评审者', encoding='utf-8')
        (root / 'agents' / 'config.json').write_text(
            json.dumps({'a': {'prompt_file': 'prompt.txt', 'tools': ['bash'], 'bash': {'timeout_seconds': 5}}}),
            encoding='utf-8',
        )
        cfg = load_agent_config(root)
        assert cfg.instructions == '你是一个评审者'
        assert cfg.tools == ['bash'] and cfg.bash_timeout == 5
        assert cfg.resolve_workdir(root) == root


@case('配置加载：按名字选 agent')
def _():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / 'agents').mkdir()
        (root / 'agents' / 'p.txt').write_text('x', encoding='utf-8')
        (root / 'agents' / 'config.json').write_text(
            json.dumps({'first': {'prompt_file': 'p.txt'}, 'second': {'prompt_file': 'p.txt'}}),
            encoding='utf-8',
        )
        assert load_agent_config(root).name == 'first'
        assert load_agent_config(root, agent_name='second').name == 'second'


@case('配置加载：错误要吵')
def _():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / 'agents').mkdir()
        (root / 'agents' / 'p.txt').write_text('x', encoding='utf-8')

        def expect_error(payload, keyword):
            (root / 'agents' / 'config.json').write_text(json.dumps(payload), encoding='utf-8')
            try:
                load_agent_config(root)
            except ConfigError as err:
                assert keyword in str(err), (keyword, str(err))
                return
            raise AssertionError(f'未按预期报错：{keyword}')

        expect_error({'a': {'prompt_file': 'p.txt', 'tools': ['rm_rf']}}, '不支持的 tool')
        expect_error({'a': {'prompt_file': 'missing.txt'}}, '找不到系统提示词')
        expect_error({'a': {'prompt_file': 'p.txt', 'workdir': '../../etc'}}, '越界')
        assert True


@case('workdir 越界被拒绝')
def _():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / 'agents').mkdir()
        (root / 'agents' / 'p.txt').write_text('x', encoding='utf-8')
        # 直接构造配置对象验证 resolve_workdir
        from config import AgentConfig
        cfg = AgentConfig(name='t', instructions='x', prompt_file='p.txt', workdir='../outside')
        try:
            cfg.resolve_workdir(root)
        except ConfigError as err:
            assert '越界' in str(err)
            return
        raise AssertionError('越界未报错')


@case('clone_repo 拒绝空 URL 并清理目录')
def _():
    try:
        clone_repo(url='', workdir=tempfile.mkdtemp())
    except RepoError as err:
        assert '上游仓库地址为空' in str(err)
        return
    raise AssertionError('空 URL 未报错')


@case('clone_repo 克隆到 /tmp 的子目录')
def _():
    import subprocess

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / 'src'
        src.mkdir()
        subprocess.run(['git', 'init', '-q', str(src)], check=True)
        (src / 'f.txt').write_text('hi', encoding='utf-8')
        subprocess.run(['git', '-C', str(src), 'add', '-A'], check=True)
        subprocess.run(
            ['git', '-C', str(src), '-c', 'user.email=a@b.c', '-c', 'user.name=a',
             'commit', '-qm', 'init', '--no-gpg-sign'],
            check=True,
        )
        target_root = Path(tempfile.mkdtemp())
        repo_dir = clone_repo(url=str(src), workdir=str(target_root))
        assert repo_dir.parent == target_root.resolve()
        assert repo_dir.name.startswith('repo-')
        assert (repo_dir / 'f.txt').read_text() == 'hi'


@case('build_context / build_prompt 用环境变量')
def _():
    saved = dict(os.environ)
    try:
        os.environ.update({
            'PROVIDER': 'github', 'TARGET_REPO': 'a/b', 'PR_NUMBER': '9',
            'UPSTREAM_REPO': 'https://github.com/a/b.git', 'TITLE': '标题',
            'BODY': '描述', 'IS_ISSUE': 'false',
        })
        ctx = build_context()
        assert ctx['repo'] == 'a/b' and ctx['pr_number'] == '9'
        assert ctx['upstream_url'] == 'https://github.com/a/b.git'
        prompt = build_prompt(ctx)
        assert 'a/b' in prompt and '标题' in prompt and '克隆' in prompt
    finally:
        os.environ.clear()
        os.environ.update(saved)


@case('build_prompt 描述超长会截断')
def _():
    ctx = {'provider': 'github', 'repo': 'a/b', 'upstream_url': '', 'pr_number': '1',
           'is_issue': False, 'title': 't', 'body': 'x' * 10_000, 'url': '', 'user': '',
           'action': '', 'base_sha': 'a' * 40, 'head_sha': 'b' * 40,
           'base_ref': 'main', 'head_ref': 'feat', 'task_file': ''}
    prompt = build_prompt(ctx)
    assert '已截断' in prompt
    assert len(prompt) < 6_000
    assert 'git diff' in prompt


if __name__ == '__main__':
    for name in PASSED:
        print(f'  ✓ {name}')
    print(f'\n{len(PASSED)} 个用例全部通过')
