import json
import re
from pathlib import Path

from payment_orchestration.domain import TRANSITIONS

ROOT = Path(__file__).parents[1]


def test_python_and_bsl_follow_canonical_transition_spec():
    spec = json.loads(
        (ROOT / "fixtures" / "payment_transition_spec.json").read_text(encoding="utf-8")
    )
    expected = {
        state: sorted(targets) for state, targets in spec["transitions"].items()
    }
    python = {
        str(state): sorted(map(str, targets)) for state, targets in TRANSITIONS.items()
    }
    assert python == expected

    bsl = (ROOT / "bsl" / "PaymentIntegration.bsl").read_text(encoding="utf-8")
    assert "Если СтарыйСтатус = НовыйСтатус Тогда Возврат Истина" in bsl
    for state, targets in expected.items():
        if targets:
            match = re.search(rf'Переходы\.Вставить\("{state}", "([^"]*)"\);', bsl)
            assert match and set(match.group(1).split(",")) == set(targets)
