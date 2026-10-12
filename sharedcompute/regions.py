"""Supported regions. A fixed list keeps job constraints and listings comparable."""

REGIONS: dict[str, str] = {
    "au-syd": "Sydney, AU",
    "au-mel": "Melbourne, AU",
    "sg-sin": "Singapore, SG",
    "jp-tyo": "Tokyo, JP",
    "in-bom": "Mumbai, IN",
    "de-fra": "Frankfurt, DE",
    "se-sto": "Stockholm, SE",
    "gb-lon": "London, GB",
    "nl-ams": "Amsterdam, NL",
    "us-east": "US East",
    "us-west": "US West",
    "ca-tor": "Toronto, CA",
    "br-sao": "São Paulo, BR",
}

ANY = "any"


def label(code: str) -> str:
    return "Any region" if code == ANY else REGIONS.get(code, code)
