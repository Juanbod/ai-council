import tempfile

import pytest


@pytest.fixture(autouse=True)
def private_tempdir(tmp_path_factory, monkeypatch):
    """Рабочие папки участников (Session.workdir) создаются во временном каталоге — в тестах свой."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path_factory.mktemp("systemp")))
