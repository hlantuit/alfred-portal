"""
permafrost block — active layer depth, ground temperature.

render(community) -> list of Notion blocks
"""


def render(community):
    # TODO: fetch permafrost / active layer data for community["id"]
    return [
        {
            "object": "block", "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": "Permafrost"}}]}
        },
        {
            "object": "block", "type": "callout",
            "callout": {
                "rich_text": [{"type": "text", "text": {"content": "Permafrost data coming soon."}}],
                "icon": {"emoji": "🧊"},
                "color": "blue_background",
            }
        },
        {"object": "block", "type": "divider", "divider": {}},
    ]
