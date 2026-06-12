import mcpbrain.agents as agents

def test_schtasks_args_embeds_home_in_action():
    home = r"C:\Users\j\.mcpbrain"
    args = agents.schtasks_args(mcpbrain_bin=r"C:\Users\j\.local\bin\mcpbrain.exe", home=home)
    action = args[args.index("/tr") + 1]
    assert "MCPBRAIN_HOME" in action and home in action

def test_schtasks_args_home_with_spaces_quoted():
    home = r"C:\Users\Josh Kemp\.mcpbrain"
    args = agents.schtasks_args(mcpbrain_bin=r"C:\Users\Josh Kemp\.local\bin\mcpbrain.exe", home=home)
    action = args[args.index("/tr") + 1]
    assert "MCPBRAIN_HOME" in action and home in action

def test_schtasks_tray_args_also_embeds_home():
    home = r"C:\Users\j\.mcpbrain"
    args = agents.schtasks_tray_args(mcpbrain_bin=r"C:\Users\j\.local\bin\mcpbrain.exe", home=home)
    action = args[args.index("/tr") + 1]
    assert "MCPBRAIN_HOME" in action and home in action

def test_schtasks_args_subcommand_present():
    args = agents.schtasks_args(mcpbrain_bin=r"C:\mcpbrain.exe", home=r"C:\h")
    assert "daemon" in args[args.index("/tr") + 1]
