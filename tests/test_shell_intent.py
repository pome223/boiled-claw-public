from src.security.shell_intent import inspect_shell_command


def test_inspect_shell_command_classifies_search():
    inspection = inspect_shell_command("rg TODO src")

    assert inspection.intent.category == "search"
    assert inspection.intent.risk == "low"
    assert inspection.validation_error() is None
    assert inspection.ast.exec_tokens == ["rg", "TODO", "src"]


def test_inspect_shell_command_blocks_shell_wrapper():
    inspection = inspect_shell_command('bash -lc "echo hi"')

    assert inspection.intent.category == "shell_wrapper"
    assert "Shell wrapper" in inspection.validation_error()


def test_inspect_shell_command_blocks_inline_interpreter_eval():
    inspection = inspect_shell_command('python -c "print(1)"')

    assert inspection.intent.category == "interpreter_eval"
    assert "Inline interpreter evaluation" in inspection.validation_error()


def test_inspect_shell_command_blocks_compact_inline_eval():
    inspection = inspect_shell_command('python3 -c"print(1)"')

    assert inspection.intent.category == "interpreter_eval"
    assert "Inline interpreter evaluation" in inspection.validation_error()


def test_inspect_shell_command_blocks_compact_shell_wrapper():
    inspection = inspect_shell_command('bash -lc"echo hi"')

    assert inspection.intent.category == "shell_wrapper"
    assert "Shell wrapper" in inspection.validation_error()


def test_inspect_shell_command_blocks_control_operator():
    inspection = inspect_shell_command("echo hi | sed s/x/y/")

    assert inspection.intent.category == "shell_features"
    assert "control operators" in inspection.validation_error()


def test_inspect_shell_command_blocks_redirection():
    inspection = inspect_shell_command("echo hi > out.txt")

    assert inspection.intent.category == "shell_features"
    assert "redirection" in inspection.validation_error()


def test_inspect_shell_command_classifies_package_test():
    inspection = inspect_shell_command("npm test")

    assert inspection.intent.category == "test"
    assert inspection.intent.risk == "low"
    assert inspection.validation_error() is None


def test_inspect_shell_command_classifies_package_build():
    inspection = inspect_shell_command("npm run build")

    assert inspection.intent.category == "build"
    assert inspection.intent.risk == "medium"
    assert inspection.validation_error() is None
