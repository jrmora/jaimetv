import re


PRIORITY = (
    "Netflix", "Amazon Prime Video", "Apple TV", "Disney Plus", "HBO Max",
    "Paramount Plus", "Crave", "Hulu", "Peacock", "Starz", "MUBI",
)


def platform_name(name):
    name = name.strip()
    base = re.sub(
        r"\s+(?:Amazon Channel|Apple TV [Cc]hannel|Roku Premium Channel)$", "", name,
        flags=re.IGNORECASE,
    )
    aliases = {
        "disney+": "Disney Plus", "acorntv": "Acorn TV",
        "paramount+": "Paramount Plus", "apple tv+": "Apple TV",
        "amazon video": "Amazon Prime Video",
        "amc plus": "AMC+", "discovery +": "Discovery+",
        "britbox": "BritBox", "hidive": "HiDive",
        "youtube premium": "YouTube", "super channel plus": "Super Channel",
        "justwatch tv free": "JustWatch TV", "justwatch tv plus": "JustWatch TV",
        "justwatch tv store": "JustWatch TV",
    }
    base = aliases.get(base.casefold(), base)
    for prefix, brand in (
        ("netflix", "Netflix"), ("amazon prime video", "Amazon Prime Video"),
        ("paramount plus", "Paramount Plus"),
    ):
        if base.casefold() == prefix or base.casefold().startswith(prefix + " "):
            return brand
    if base in {"Apple TV Store", "Apple TV", "Apple TV Plus"}:
        return "Apple TV"
    return base


def ordered_platforms(names):
    priority = {name: index for index, name in enumerate(PRIORITY)}
    return sorted({platform_name(name) for name in names},
                  key=lambda name: (priority.get(name, len(priority)), name.casefold()))
