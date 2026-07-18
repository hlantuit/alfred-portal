"""
erosion block — coastal erosion rates from ACD / Nylen-derived sources.

render(community) -> list of Notion blocks
"""


def render(community):
    # TODO: pull erosion rate data for community["id"] from ACD dataset
    return [
        {
            "object": "block", "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": "Coastal Erosion"}}]}
        },
        {
            "object": "block", "type": "callout",
            "callout": {
                "rich_text": [{"type": "text", "text": {"content": "Erosion data coming soon."}}],
                "icon": {"emoji": "🌊"},
                "color": "blue_background",
            }
        },
        {"object": "block", "type": "divider", "divider": {}},
    ]
