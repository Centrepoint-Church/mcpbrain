"""The home directory is embedded in the shim path and shim content, not in /tr inline."""
import mcpbrain.agents as agents


def test_schtasks_args_embeds_home_in_shim_path():
    home = r"C:\Users\j\.mcpbrain"
    args = agents.schtasks_args(mcpbrain_bin=r"C:\Users\j\.local\bin\mcpbrain.exe", home=home)
    action = args[args.index("/tr") + 1]
    # The home path appears in the shim path (home/agents/<task>.vbs) inside /tr.
    assert home in action


def test_schtasks_args_home_with_spaces_in_shim_path():
    home = r"C:\Users\Josh Kemp\.mcpbrain"
    args = agents.schtasks_args(mcpbrain_bin=r"C:\Users\Josh Kemp\.local\bin\mcpbrain.exe", home=home)
    action = args[args.index("/tr") + 1]
    assert home in action


def test_schtasks_tray_args_also_embeds_home():
    home = r"C:\Users\j\.mcpbrain"
    args = agents.schtasks_tray_args(mcpbrain_bin=r"C:\Users\j\.local\bin\mcpbrain.exe", home=home)
    action = args[args.index("/tr") + 1]
    assert home in action


def test_schtasks_args_shim_content_has_home_and_subcommand():
    # The shim content (not /tr) is where MCPBRAIN_HOME and the subcommand live.
    vbs = agents._win_shim_content(
        mcpbrain_bin=r"C:\mcpbrain.exe", home=r"C:\h", subcommand="daemon",
        python_bin=r"C:\py\pythonw.exe")
    assert "MCPBRAIN_HOME" in vbs and "daemon" in vbs
