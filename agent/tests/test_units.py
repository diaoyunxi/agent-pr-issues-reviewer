"""不需要网络与模型的单元测试：配置加载、URL 脱敏、环境清洗、bash 工具。

跑法：`python agent/tests/test_units.py`（不依赖 pytest，CI 里也能直接跑）。
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from config import ConfigError, build_agent_config, AgentConfig  # noqa: E402
from repo import (  # noqa: E402
    RepoError,
    _authenticated_url,
    checkout_head,
    clone_repo,
    mask_url,
    sanitize_env,
)
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


@case('配置加载：写死的 review 配置')
def _():
    cfg = build_agent_config('review')
    assert cfg.mode == 'review'
    assert cfg.name == 'code-reviewer'
    assert '严格的资深代码评审者' in cfg.instructions
    assert cfg.tools == ['bash'] and cfg.bash_timeout == 120
    assert cfg.allow_inline_comments is True
    assert cfg.prompt_file == 'builtin:review'


@case('配置加载：写死的 work 配置')
def _():
    cfg = build_agent_config('work')
    assert cfg.mode == 'work'
    assert cfg.name == 'code-worker'
    assert '执行任务的工程师' in cfg.instructions
    assert cfg.allow_inline_comments is True


@case('配置加载：MODE 环境变量兜底，非法模式要吵')
def _():
    os.environ['MODE'] = 'review'
    try:
        assert build_agent_config().mode == 'review'
        os.environ['MODE'] = 'chat'
        try:
            build_agent_config()
        except ConfigError as err:
            assert '不支持的模式' in str(err)
        else:
            raise AssertionError('非法模式未报错')
    finally:
        os.environ.pop('MODE', None)


@case('build_prompt 的 work 模式带出用户要求')
def _():
    ctx = {'provider': 'github', 'repo': 'a/b', 'upstream_url': '', 'pr_number': '9',
           'is_issue': False, 'title': 't', 'body': '描述', 'url': '', 'user': 'u',
           'action': 'created', 'mode': 'work', 'instruction': '把 README 里的 x 改成 y',
           'base_sha': '', 'head_sha': '', 'base_ref': '', 'head_ref': 'feat', 'task_file': ''}
    prompt = build_prompt(ctx)
    assert '用户的要求' in prompt and '把 README 里的 x 改成 y' in prompt
    # review 模式不带要求段落
    ctx['mode'] = 'review'
    assert '用户的要求' not in build_prompt(ctx)


@case('build_context 读到 mode / instruction')
def _():
    saved = dict(os.environ)
    try:
        os.environ.update({'MODE': 'work', 'INSTRUCTION': '改个 bug'})
        ctx = build_context()
        assert ctx['mode'] == 'work' and ctx['instruction'] == '改个 bug'
    finally:
        os.environ.clear()
        os.environ.update(saved)


@case('workdir 越界被拒绝')
def _():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # 直接构造配置对象验证 resolve_workdir
        cfg = AgentConfig(name='t', instructions='x', prompt_file='builtin', workdir='../outside')
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


@case('clone_repo 完整克隆到 /tmp 的子目录（全量历史 + 所有分支）')
def _():
    import subprocess

    def git(*args, cwd=None):
        subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True)

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / 'src'
        src.mkdir()
        git('init', '-q', str(src))
        git('-C', str(src), 'config', 'user.email', 'a@b.c')
        git('-C', str(src), 'config', 'user.name', 'a')
        (src / 'f.txt').write_text('v1', encoding='utf-8')
        git('-C', str(src), 'add', '-A')
        git('-C', str(src), 'commit', '-qm', 'c1', '--no-gpg-sign')
        (src / 'f.txt').write_text('v2', encoding='utf-8')
        git('-C', str(src), 'add', '-A')
        git('-C', str(src), 'commit', '-qm', 'c2', '--no-gpg-sign')
        # 另起一条分支，验证 --no-single-branch 把它的 ref 也拉了下来
        git('-C', str(src), 'checkout', '-qb', 'feat')
        (src / 'g.txt').write_text('feat', encoding='utf-8')
        git('-C', str(src), 'add', '-A')
        git('-C', str(src), 'commit', '-qm', 'c3', '--no-gpg-sign')

        target_root = Path(tempfile.mkdtemp())
        repo_dir = clone_repo(url=str(src), workdir=str(target_root))
        assert repo_dir.parent == target_root.resolve()
        assert repo_dir.name.startswith('repo-')
        assert (repo_dir / 'f.txt').read_text() == 'v2'

        # 完整克隆：提交历史不是一个孤立的提交
        log = subprocess.run(
            ['git', '-C', str(repo_dir), 'rev-list', '--count', 'HEAD'],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert int(log) >= 2, f'历史被截断，只有 {log} 个提交'

        # 完整克隆：远端所有分支的 ref 都在（非单分支克隆）
        refs = subprocess.run(
            ['git', '-C', str(repo_dir), 'branch', '-r'],
            check=True, capture_output=True, text=True,
        ).stdout
        assert 'origin/feat' in refs, f'缺少其他分支的 remote ref：{refs}'


@case('checkout_head 按 sha 检出，sha 不可达时回退分支')
def _():
    import subprocess

    def git(*args, cwd=None):
        subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True)

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / 'src'
        src.mkdir()
        git('init', '-q', str(src))
        git('-C', str(src), 'config', 'user.email', 'a@b.c')
        git('-C', str(src), 'config', 'user.name', 'a')
        (src / 'f.txt').write_text('v1', encoding='utf-8')
        git('-C', str(src), 'add', '-A')
        git('-C', str(src), 'commit', '-qm', 'c1', '--no-gpg-sign')
        first = subprocess.run(
            ['git', '-C', str(src), 'rev-parse', 'HEAD'],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        (src / 'f.txt').write_text('v2', encoding='utf-8')
        git('-C', str(src), 'add', '-A')
        git('-C', str(src), 'commit', '-qm', 'c2', '--no-gpg-sign')
        git('-C', str(src), 'checkout', '-qb', 'feat')

        target_root = Path(tempfile.mkdtemp())
        repo_dir = clone_repo(url=str(src), workdir=str(target_root))

        # 按 sha 检出旧提交
        checkout_head(repo_dir, first)
        assert (repo_dir / 'f.txt').read_text() == 'v1'

        # 假 sha → 回退到分支
        checkout_head(repo_dir, '0' * 40, 'feat')
        head = subprocess.run(
            ['git', '-C', str(repo_dir), 'rev-parse', '--abbrev-ref', 'HEAD'],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert head == 'feat', f'未回退到分支，当前在 {head}'


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
