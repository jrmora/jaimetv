import json
import re
import unicodedata


KIDS_CATEGORIES = {
    "superheroes": "Superhéroes",
    "anime": "Anime",
    "animation_hits": "Cine animado",
    "family": "En familia",
}


def normalized(value):
    return "".join(
        char for char in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(char)
    )


def automatic_kids_categories(name, payload):
    metadata = json.loads(payload) if payload else {}
    if metadata.get("status") != "matched":
        return "[]"
    genres = {normalized(value) for value in metadata.get("genres", [])}
    keywords = {normalized(value) for value in metadata.get("keywords", [])}
    countries = {normalized(value) for value in metadata.get("countries", [])}
    companies = " ".join(normalized(value) for value in metadata.get("companies", []))
    titles = " ".join(normalized(value) for value in (
        name, metadata.get("title"), metadata.get("original_title"),
    ) if value)
    animated = bool(genres & {"animacion", "animation"})
    family = bool(genres & {"familia", "family"})
    superhero = bool(keywords & {
        "superhero", "superheroes", "super hero",
        "marvel cinematic universe (mcu)", "dc extended universe (dceu)",
    }) or bool(re.search(r"\b(?:marvel|dc comics|dc entertainment|dc films|dc studios)\b", companies))
    superhero = superhero or bool(re.search(
        r"\b(?:avengers|vengadores|batman|superman|spider.man|hombre arana|wonder woman|"
        r"mujer maravilla|justice league|liga de la justicia|iron man|capitan america|"
        r"captain america|black panther|guardianes de la galaxia|guardians of the galaxy|"
        r"aquaman|shazam|x.men|los increibles|the incredibles|big hero 6)\b", titles,
    ))
    asian_animation = animated and (
        "anime" in keywords
        or metadata.get("original_language") in {"ja", "ko", "zh", "cn"}
        or bool(countries & {"japan", "japon", "china", "south korea", "corea del sur", "taiwan", "hong kong"})
        or bool(re.search(r"\b(?:toei animation|studio ghibli|madhouse|kyoto animation|"
                          r"studio bones|ufotable|mappa|co ?mix wave)\b", companies))
    )
    family_animation = animated and family and not asian_animation
    sunday_family = (family and not animated) or bool(re.search(
        r"\b(?:la guarderia de papa|daddy day care|daddy day camp|cheaper by the dozen|"
        r"mas barato por docena|doce en casa|mrs doubtfire|papa por siempre|"
        r"la senora doubtfire|the parent trap|juego de gemelas|tu a londres y yo a california)\b",
        titles,
    ))
    values = (superhero, asian_animation, family_animation, sunday_family)
    return json.dumps([key for key, enabled in zip(KIDS_CATEGORIES, values) if enabled])
