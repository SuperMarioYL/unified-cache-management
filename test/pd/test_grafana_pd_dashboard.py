import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


DASHBOARD = Path(__file__).resolve().parents[2] / "examples/metrics/grafana_pd.json"


def test_every_metric_links_to_its_own_explanation_and_back():
    dashboard = json.loads(DASHBOARD.read_text())
    panels = [
        panel
        for item in dashboard["panels"]
        for panel in (item, *item.get("panels", []))
    ]
    by_id = {panel["id"]: panel for panel in panels}
    assert len(by_id) == len(panels)

    def destination(link):
        url = urlsplit(link["url"])
        assert not url.netloc
        assert url.path == "d/${__dashboard.uid}/pd"
        panel_id = parse_qs(url.query)["viewPanel"][0].removeprefix("panel-")
        return by_id[int(panel_id)]

    notes = set()
    for metric in (panel for panel in panels if panel.get("targets")):
        (link,) = metric["links"]
        assert link in metric["fieldConfig"]["defaults"]["links"]
        note = destination(link)
        assert note["type"] == "text"
        assert link["title"] == note["title"]
        assert note["id"] not in notes
        notes.add(note["id"])
        assert destination(note["links"][0]) is metric

        content = note["options"]["content"]
        assert metric["title"] in content
        for target in metric["targets"]:
            # Each P/D query selects its Prometheus series by name and labels.
            selectors = re.findall(r"([a-zA-Z_:][a-zA-Z0-9_:]*)\{", target["expr"])
            assert selectors, target["expr"]
            for selector in selectors:
                assert f"`{selector}`" in content, (metric["title"], selector)

    assert len(notes) == 28
