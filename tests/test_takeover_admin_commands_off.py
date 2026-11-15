"""乗っ取り対策 第1段（2026-11-15）: 管理者向けの命令4つと出品の命令を読み込まない。"""

import inspect

from claude_discord import main


def test_switches_are_off():
    assert main.ADMIN_COMMANDS_ENABLED is False
    assert main.LISTING_COMMAND_ALLOWED is False


def test_admin_cogs_are_built_only_when_enabled_and_added_only_if_built():
    src = inspect.getsource(main)
    for var, cls in (
        ("shell_exec_cog", "ShellExecCog"),
        ("order_cog", "OrderCommandCog"),
        ("goq_upload_cog", "GoqUploadCommandCog"),
        ("haiban_cog", "HaibanCommandCog"),
    ):
        assert f"{var} = {cls}(bot) if ADMIN_COMMANDS_ENABLED else None" in src, var
        assert f"if {var} is not None:\n            await bot.add_cog({var})" in src, var
        guarded = f"if {var} is not None:\n            await bot.add_cog({var})"
        assert f"        await bot.add_cog({var})\n" not in src.replace(guarded, ""), var


def test_listing_needs_code_switch_and_env():
    src = inspect.getsource(main)
    assert 'if LISTING_COMMAND_ALLOWED and os.getenv("SHUPPIN_ENABLED"' in src
