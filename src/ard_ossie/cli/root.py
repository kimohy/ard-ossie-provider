from __future__ import annotations

import typer

from ard_ossie.cli import (
    changeset,
    github,
    impact,
    model,
    parse,
    registry,
    release,
    validate,
    workflow,
)
from ard_ossie.cli.history import diff_command, history, show
from ard_ossie.cli.process import process

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
app.command()(process)
app.command()(history)
app.command()(show)
app.command("diff")(diff_command)
app.add_typer(registry.app, name="registry")
app.add_typer(impact.app, name="impact")
app.add_typer(changeset.app, name="changeset")
app.add_typer(release.app, name="release")
app.add_typer(parse.app, name="parse")
app.add_typer(model.app, name="model")
app.add_typer(validate.app, name="validate")
app.add_typer(github.app, name="github")
app.add_typer(workflow.app, name="workflow")
