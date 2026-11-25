from dataclasses import dataclass, replace
from hashlib import sha256
import re
from urllib.parse import urlsplit


SOCIAL_TYPES = {
    "vkontakte", "vk", "odnoklassniki", "ok", "telegram", "whatsapp",
    "viber", "instagram", "facebook", "twitter", "youtube", "tiktok",
    "linkedin", "pinterest", "rutube", "dzen", "max", "skype", "discord",
    "threads", "snapchat", "wechat", "weibo", "social", "messenger",
}
SOCIAL_DOMAINS = (
    "vk.com", "vk.ru", "ok.ru", "t.me", "telegram.me", "whatsapp.com",
    "wa.me", "viber.com", "instagram.com", "facebook.com", "fb.com",
    "twitter.com", "x.com", "youtube.com", "youtu.be", "tiktok.com",
    "linkedin.com", "pinterest.com", "rutube.ru", "dzen.ru", "max.ru",
    "threads.net", "threads.com", "discord.com", "discord.gg",
    "weixin.qq.com", "wechat.com", "snapchat.com", "weibo.com",
)


def normalized(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").split())


def text(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def objects(value: object) -> list[dict]:
    return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []


def object_id(value: object) -> str:
    if isinstance(value, str):
        number = value.split("_", 1)[0]
        if re.fullmatch(r"[0-9]{1,24}", number):
            return number
    return ""


def web_host(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    if parsed.scheme in ("http", "https") and parsed.username is None and parsed.password is None:
        return parsed.hostname or ""
    return ""


def social_url(url: str) -> bool:
    host = web_host(url)
    return any(host == domain or host.endswith("." + domain) for domain in SOCIAL_DOMAINS)


def phone_identity(value: str) -> str:
    digits = re.sub(r"[^0-9]", "", value)
    if len(digits) == 11 and digits.startswith("8"):
        return "7" + digits[1:]
    return digits


def unique_phones(values: list[str]) -> list[str]:
    result: dict[str, str] = {}
    for value in values:
        digits = phone_identity(value)
        if 5 <= len(digits) <= 18:
            result.setdefault(digits, value.removeprefix("tel:"))
    return list(result.values())


def address_words(address: str) -> str:
    words = re.findall(r"[\w/-]+", normalized(address))
    expanded = {"ул": "улица", "просп": "проспект", "пр-кт": "проспект", "пер": "переулок"}
    return " ".join(sorted(expanded.get(word, word) for word in words if word != "россия"))


@dataclass(frozen=True)
class Company:
    id: str
    name: str
    address: str | None
    phones: list[str]
    socials: list[str]
    aliases: frozenset[str]

    def public(self) -> dict:
        return {
            "id": self.id, "name": self.name, "address": self.address,
            "phones": self.phones, "socials": self.socials,
        }


def company(id_: str, name: str, address: str | None, phones: list[str], socials: list[str],
            extra_ids: tuple[str, ...] = ()) -> Company:
    title = " ".join(re.findall(r"\w+", normalized(name)))
    phones = unique_phones(phones)
    signatures = [f"name_phone\0{title}\0{phone_identity(phone)}" for phone in phones if len(phone_identity(phone)) >= 10]
    if address and re.search(r"\d", address):
        signatures.append(f"name_address\0{title}\0{address_words(address)}")
    hashes = ("identity:" + sha256(value.encode()).hexdigest() for value in signatures)
    return Company(id_, name, address, phones, list(dict.fromkeys(socials)), frozenset((id_, *extra_ids, *hashes)))


def merge_companies(companies: list[Company]) -> list[Company]:
    groups: dict[str, Company] = {}
    alias_to_group: dict[str, str] = {}
    for entry in companies:
        matching_ids = {alias_to_group[key] for key in entry.aliases if key in alias_to_group}
        matching_groups = [value for group_id, value in groups.items() if group_id in matching_ids]
        combined = [*matching_groups, entry]
        primary = combined[0]
        merged = replace(
            primary,
            address=next((record.address for record in combined if record.address), None),
            phones=unique_phones([phone for record in combined for phone in record.phones]),
            socials=list(dict.fromkeys(social for record in combined for social in record.socials)),
            aliases=frozenset(alias for record in combined for alias in record.aliases),
        )
        for group in matching_groups[1:]:
            del groups[group.id]
        groups[merged.id] = merged
        alias_to_group.update(dict.fromkeys(merged.aliases, merged.id))
    return list(groups.values())


def from_2gis(item: dict, default_city: str) -> Company | None:
    org = item.get("org") if isinstance(item.get("org"), dict) else {}
    name_ex = item.get("name_ex") if isinstance(item.get("name_ex"), dict) else {}
    name = text(org.get("name")) or text(name_ex.get("primary")) or text(item.get("name"))
    branch_id = object_id(item.get("id"))
    org_id = object_id(org.get("id"))
    if (item.get("type") != "branch" or item.get("is_deleted") or not name or not branch_id):
        return None

    address = text(item.get("full_address_name"))
    if not address and (street := text(item.get("address_name"))):
        cities = [text(part.get("name")) for part in objects(item.get("adm_div")) if part.get("type") == "city"]
        city = next((city for city in cities if city), default_city)
        address = f"{city}, {street}" if city and not normalized(street).startswith(normalized(city) + ",") else street

    phones: list[str] = []
    socials: list[str] = []
    for group in objects(item.get("contact_groups")):
        for contact in objects(group.get("contacts")):
            kind = text(contact.get("type")).lower()
            value = text(contact.get("value")) or text(contact.get("text"))
            if kind == "phone":
                phones.append(value)
                continue
            url = text(contact.get("url"))
            if not web_host(url):
                url = value
            if ((kind in SOCIAL_TYPES and web_host(url)) or social_url(url)):
                socials.append(url)
            elif kind in SOCIAL_TYPES and value and ":" not in value:
                socials.append(value)
    branch = f"2gis:branch:{branch_id}"
    identity = f"2gis:org:{org_id}" if org_id else branch
    return company(identity, name, address or None, phones, socials, (branch,))
