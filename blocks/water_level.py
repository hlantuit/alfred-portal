"""
water_level block — water level / surge from TOPAZ6 or tide gauge.

render(community) -> list of Notion blocks
"""


def render(community):
    # TODO: fetch water level for community["lat"], community["lon"]
    return [
        {
            "object": "block", "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": "Water Level"}}]}
        },
        {
            "object": "block", "type": "callout",
            "callout": {
                "rich_text": [{"type": "text", "text": {"content": "Water level data coming soon."}}],
                "icon": {"emoji": "📊"},
                "color": "blue_background",
            }
        },
        {"object": "block", "type": "divider", "divider": {}},
    ]
