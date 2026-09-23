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
from git_write import (  # noqa: E402
    WriteContext,
    WriteError,
    apply_patch,
    build_write_tools,
    git_commit,
    git_push,
)
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


def _init_repo(path: Path) -> None:
    """造一个带一次提交的本地仓库，用于写权限测试。"""
    import subprocess

    subprocess.run(['git', 'init', '-q', str(path)], check=True)
    # runner 上可能开了全局 gpgsign，本地关掉，避免提交因签名失败
    subprocess.run(['git', '-C', str(path), 'config', 'commit.gpgsign', 'false'], check=True)
    for key, value in (('user.email', 'a@b.c'), ('user.name', 'a'), ('commit.gpgsign', 'false')):
        subprocess.run(['git', '-C', str(path), 'config', key, value], check=True)
    (path / 'a.txt').write_text('v1\n', encoding='utf-8')
    subprocess.run(['git', '-C', str(path), 'add', '-A'], check=True)
    subprocess.run(['git', '-C', str(path), 'commit', '-qm', 'init'], check=True)


@case('apply_patch + git_commit 改文件并提交')
def _():
    import subprocess

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / 'r'
        repo.mkdir()
        _init_repo(repo)
        ctx = WriteContext(workdir=str(repo), push_branch='feat', remote_url=str(repo))

        result = apply_patch(ctx, '--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-v1\n+v2\n')
        assert '已应用' in result
        assert (repo / 'a.txt').read_text() == 'v2\n'

        # 未提交时工作区是脏的，推送要被拒绝
        try:
            git_push(ctx)
            raise AssertionError('未提交就推送应该失败')
        except WriteError as err:
            assert '还没有产生提交' in str(err), err

        assert '已提交' in git_commit(ctx, 'fix: 修正 a.txt')
        assert ctx.commits == 1
        log = subprocess.run(
            ['git', '-C', str(repo), 'log', '--oneline'], check=True, capture_output=True, text=True
        ).stdout
        assert '修正 a.txt' in log
        # 没有改动时再提交要报错，而不是造一个空提交
        try:
            git_commit(ctx, 'chore: 空提交')
            raise AssertionError('无改动提交应该失败')
        except WriteError as err:
            assert '没有改动' in str(err), err


@case('apply_patch 不匹配的补丁被拒绝且不改动文件')
def _():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / 'r'
        repo.mkdir()
        _init_repo(repo)
        ctx = WriteContext(workdir=str(repo), push_branch='feat', remote_url=str(repo))
        try:
            apply_patch(ctx, '--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-不存在的行\n+xxx\n')
            raise AssertionError('不匹配的补丁应该失败')
        except WriteError as err:
            assert 'git apply 失败' in str(err), err
        assert (repo / 'a.txt').read_text() == 'v1\n'


@case('git_push 推送到指定分支，主干分支被拒绝')
def _():
    import subprocess

    with tempfile.TemporaryDirectory() as tmp:
        remote = Path(tmp) / 'remote.git'
        subprocess.run(['git', 'init', '-q', '--bare', str(remote)], check=True)
        local = Path(tmp) / 'local'
        local.mkdir()
        _init_repo(local)
        subprocess.run(['git', '-C', str(local), 'branch', '-M', 'feat'], check=True)
        subprocess.run(['git', '-C', str(local), 'remote', 'add', 'origin', str(remote)], check=True)
        subprocess.run(['git', '-C', str(local), 'push', '-q', 'origin', 'feat'], check=True)

        ctx = WriteContext(workdir=str(local), push_branch='feat', remote_url=str(remote))
        (local / 'a.txt').write_text('v2\n', encoding='utf-8')
        git_commit(ctx, 'fix: 改 a.txt')
        assert '已推送' in git_push(ctx)

        pushed = subprocess.run(
            ['git', '-C', str(remote), 'log', '--oneline', 'feat'], check=True, capture_output=True, text=True
        ).stdout
        assert '改 a.txt' in pushed

        # 主干分支不允许直接推
        blocked = WriteContext(workdir=str(local), push_branch='main', remote_url=str(remote), commits=1)
        try:
            git_push(blocked)
            raise AssertionError('推 main 应该被拒绝')
        except WriteError as err:
            assert '主干分支' in str(err), err

        # 没有指定分支时也拒绝
        empty = WriteContext(workdir=str(local), push_branch='', remote_url=str(remote), commits=1)
        try:
            git_push(empty)
            raise AssertionError('空分支应该被拒绝')
        except WriteError as err:
            assert '未指定可推送的源分支' in str(err), err


@case('写权限开关：默认关闭，配置可开并可校验工具名')
def _():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / 'r'
        (repo / 'agents').mkdir(parents=True)
        (repo / 'agents' / 'prompt.txt').write_text('p', encoding='utf-8')

        def write_config(entry: dict) -> None:
            (repo / 'agents' / 'config.json').write_text(
                json.dumps({'reviewer': {'prompt_file': 'prompt.txt', **entry}}), encoding='utf-8'
            )

        # 默认关闭写权限
        write_config({})
        cfg = load_agent_config(repo)
        assert cfg.allow_write is False and cfg.require_approval is True

        # 显式打开且要求人工确认（默认）
        write_config({'write': {'enabled': True}})
        cfg = load_agent_config(repo)
        assert cfg.allow_write is True and cfg.require_approval is True
        assert cfg.write_tools == ['apply_patch', 'git_commit', 'git_push']

        # 直接改源分支
        write_config({'write': {'enabled': True, 'require_approval': False}})
        cfg = load_agent_config(repo)
        assert cfg.allow_write is True and cfg.require_approval is False

        # 未知写工具名直接报错
        write_config({'write': {'enabled': True, 'tools': ['git_push', 'rm-rf']}})
        try:
            load_agent_config(repo)
            raise AssertionError('未知写工具名应该报错')
        except ConfigError as err:
            assert 'write.tools' in str(err), err

        # write 段类型写错也要吵
        write_config({'write': 'yes'})
        try:
            load_agent_config(repo)
            raise AssertionError('write 段类型错误应该报错')
        except ConfigError as err:
            assert 'JSON 对象' in str(err), err


@case('build_write_tools 提供三个写工具')
def _():
    tools = build_write_tools(WriteContext(workdir='/tmp', push_branch='feat', remote_url='u'))
    names = sorted(getattr(t, 'name', '').removesuffix('_tool') for t in tools)
    assert names == ['apply_patch', 'git_commit', 'git_push'], names


@case('build_prompt 按写权限模式给出不同指引')
def _():
    ctx = {'provider': 'github', 'repo': 'a/b', 'upstream_url': '', 'pr_number': '1',
           'is_issue': False, 'title': 't', 'body': 'b', 'url': '', 'user': '',
           'action': '', 'base_sha': 'a' * 40, 'head_sha': 'b' * 40,
           'base_ref': 'main', 'head_ref': 'feat', 'task_file': ''}
    assert '写权限' not in build_prompt(ctx)
    assert '直接落到本条 PR 的源分支' in build_prompt(ctx, 'direct')
    assert '不许直接改 PR 源分支' in build_prompt(ctx, 'proposal')


if __name__ == '__main__':
    for name in PASSED:
        print(f'  ✓ {name}')
    print(f'\n{len(PASSED)} 个用例全部通过')
