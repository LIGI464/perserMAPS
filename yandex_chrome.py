"""Search public Yandex Maps pages in Chrome and parse their business cards."""

import argparse
from contextlib import suppress
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import ElementClickInterceptedException, StaleElementReferenceException, TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

from companies import social_url, text, unique_phones, web_host


LISTING_SELECTOR = "a.search-snippet-view__link-overlay, a[href*='/maps/org/']"
BUSINESS_RESULT = ".search-business-snippet-view"
BUSINESS_RESULT_TITLE = ".search-business-snippet-view__title"
BUSINESS_RESULT_ADDRESS = ".search-business-snippet-view__address"
SEARCH_INPUT = "input.search-form-view__input, .search-form-view input, input[role='searchbox'], input[aria-label='Поиск'], input[aria-label='Search']"
SEARCH_BUTTON = ".small-search-form-view__button, .search-form-view button[type='submit'], button[aria-label='Найти'], button[aria-label='Search']"
HEADING_SELECTOR = ".orgpage-header-view__header, .business-card-title-view__title, h1[itemprop='name']"
ADDRESS_SELECTOR = ".business-contacts-view__address-link, .card-contacts-view__address"
PHONE_SELECTOR = ".card-phones-view__number, .orgpage-phones-view__phone-number, [itemprop='telephone']"
TEL_SELECTOR = "a[href^='tel:' i]"
SOCIAL_BUTTON_SELECTOR = ".business-contacts-view__social-button a[href]"
PHONE_DETAILS = f"{PHONE_SELECTOR}, {TEL_SELECTOR}"
CONTACT_DETAILS = f"{ADDRESS_SELECTOR}, {PHONE_DETAILS}, {SOCIAL_BUTTON_SELECTOR}"
CARD_DETAIL_SELECTOR = f"{HEADING_SELECTOR}, .business-contacts-view, {CONTACT_DETAILS}"
CONTACT_SELECTORS = (
    "[role='tab']", "button", "[role='button']",
)
CONTACT_BUTTON = re.compile(r"^(?:контакты|показать(?: все)? телефон(?:ы)?|ещ[её]\s+\d+\s+телефон\w*)$", re.I)
EMPTY_MESSAGE = re.compile(r"ничего не найдено|не удалось ничего найти|результатов нет", re.I)
CHALLENGE_MESSAGE = re.compile(r"captcha|капча|вы не робот|вы робот\?|доступ ограничен|access denied", re.I)
YANDEX_ORG = re.compile(r"^/maps/org/(?:[^/]+/)?([0-9]{1,24})(?:/|$)")


@dataclass(frozen=True)
class Listing:
    url: str
    name: str = ""
    address: str = ""


class CardError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def create_driver(binary: str, driver_binary: str | None = None):
    options = Options()
    options.binary_location = binary
    options.page_load_strategy = "eager"
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1280,900")
    options.add_argument("--disable-dev-shm-usage")
    if driver_binary:
        return webdriver.Chrome(options=options, service=Service(driver_binary))
    return webdriver.Chrome(options=options)


def org_url(url: str) -> tuple[str, str] | None:
    host = web_host(url)
    if host not in {"yandex.ru", "www.yandex.ru", "yandex.com", "www.yandex.com"}:
        return None
    match = YANDEX_ORG.match(urlsplit(url).path)
    if match:
        identity = match[1]
        return identity, f"https://{host}/maps/org/{identity}/"
    return None


def check_page(driver, *, inspect_body: bool = False) -> None:
    path = urlsplit(driver.current_url).path.lower()
    title = driver.title or ""
    if "showcaptcha" in path or CHALLENGE_MESSAGE.search(title):
        raise CardError("access_restricted", "Яндекс показал ограничение доступа.")
    if inspect_body or not title:
        body = driver.execute_script("return (document.body?.innerText || '').trim().slice(0, 32)")
        if isinstance(body, str) and body.casefold() == "limited":
            raise CardError("access_restricted", "Карты Яндекса вернули страницу limited.")


def listing_links(driver) -> tuple[list, dict[str, Listing]]:
    check_page(driver)
    candidates = driver.execute_script("""
        const [resultSelector, titleSelector, addressSelector, legacySelector] = arguments;
        const modern = [...document.querySelectorAll(resultSelector)]
            .filter((row) => row.getClientRects().length)
            .flatMap((row) => [...row.querySelectorAll('a[href*="/org/"]')]);
        const links = modern.length ? modern : [...document.querySelectorAll(legacySelector)];
        return links.map((anchor) => {
            const row = anchor.closest(resultSelector);
            return {
                anchor, href: anchor.href,
                name: row?.querySelector(titleSelector)?.innerText || '',
                address: row?.querySelector(addressSelector)?.innerText || '',
            };
        });
    """, BUSINESS_RESULT, BUSINESS_RESULT_TITLE, BUSINESS_RESULT_ADDRESS, LISTING_SELECTOR)
    anchors = []
    links: dict[str, Listing] = {}
    for candidate in candidates:
        if match := org_url(candidate["href"]):
            identity, url = match
            links.setdefault(identity, Listing(url, text(candidate["name"]), text(candidate["address"])))
            anchors.append(candidate["anchor"])
    return anchors, links


def gather_links(driver, city: str, query: str, count: int) -> dict[str, Listing]:
    driver.get("https://yandex.ru/maps")

    def search_field(page):
        check_page(page)
        return next((field for field in page.find_elements(By.CSS_SELECTOR, SEARCH_INPUT)
                     if field.is_displayed() and field.is_enabled()), None)

    try:
        field = WebDriverWait(driver, 10).until(search_field)
    except TimeoutException:
        check_page(driver, inspect_body=True)
        raise CardError("search_layout", "Карты Яндекса не показали поле поиска.") from None

    field.clear()
    field.send_keys(f"{city} {query}")
    submit = next((button for button in driver.find_elements(By.CSS_SELECTOR, SEARCH_BUTTON)
                   if button.is_displayed() and button.is_enabled()), None)
    try:
        if submit is None:
            field.send_keys(Keys.ENTER)
        else:
            submit.click()
    except (ElementClickInterceptedException, StaleElementReferenceException):
        raise CardError("search_layout", "Кнопка поиска Яндекса недоступна на открытой странице.") from None
    try:
        links = WebDriverWait(driver, 15).until(lambda page: listing_links(page)[1])
    except TimeoutException:
        check_page(driver, inspect_body=True)
        if EMPTY_MESSAGE.search(driver.find_element(By.TAG_NAME, "body").text):
            return {}
        raise CardError("search_layout", "Яндекс не показал ссылки на карточки. Нужна проверка страницы поиска.") from None

    for _ in range((count + 9) // 10):
        if len(links) >= count:
            break
        anchors, _ = listing_links(driver)
        if not anchors:
            break
        driver.execute_script("arguments[0].scrollIntoView({block: 'end'})", anchors[-1])
        try:
            more = WebDriverWait(driver, 2).until(
                lambda page: {key: listing for key, listing in listing_links(page)[1].items() if key not in links}
            )
        except TimeoutException:
            break
        links.update(more)
    return dict(list(links.items())[:count])


def click_public_contacts(driver) -> None:
    for contacts_tab, selector in ((True, CONTACT_DETAILS), (False, PHONE_DETAILS)):
        for control in driver.find_elements(By.CSS_SELECTOR, ", ".join(CONTACT_SELECTORS)):
            label = text(control.text or control.get_attribute("aria-label"))
            if (CONTACT_BUTTON.fullmatch(label) and (label.casefold() == "контакты") == contacts_tab
                    and control.is_displayed() and control.is_enabled()):
                previous = driver.find_elements(By.CSS_SELECTOR, selector)
                control.click()
                with suppress(TimeoutException):
                    WebDriverWait(driver, 2).until(
                        lambda page: page.find_elements(By.CSS_SELECTOR, selector) != previous
                    )
                check_page(driver, inspect_body=True)
                break


def html_value(element) -> str:
    return text(element.get("content") or element.get_text(" ")) if element else ""


def parse_card(html: str | bytes, identity: str | None = None, listing: Listing | None = None) -> dict | None:
    soup = BeautifulSoup(html, "lxml")
    if identity is None:
        for element in soup.select("link[rel='canonical'][href], meta[property='og:url'][content]"):
            if match := org_url(text(element.get("href") or element.get("content"))):
                identity = match[0]
                break
    name = html_value(soup.select_one(HEADING_SELECTOR))
    if not name and listing:
        name = listing.name
    if not identity or not name:
        return None
    address = html_value(soup.select_one(ADDRESS_SELECTOR))
    if not address:
        street = html_value(soup.select_one("[itemprop='streetAddress']"))
        locality = html_value(soup.select_one("[itemprop='addressLocality']"))
        address = ", ".join(part for part in (locality, street) if part)
    if not address and listing:
        address = listing.address

    phones = [html_value(element) for element in soup.select(PHONE_SELECTOR)]
    phones.extend(text(anchor.get("href"))[4:] for anchor in soup.select(TEL_SELECTOR))
    social_links = soup.select(
        f".business-contacts-view a[href], {SOCIAL_BUTTON_SELECTOR}, "
        ".card-contacts-view a[href], a[itemprop='sameAs'][href], link[itemprop='sameAs'][href], "
        "a.button._view_secondary-gray._ui._size_medium._link[href]"
    )
    socials = [text(link.get("href")) for link in social_links]
    socials = ["https:" + url if url.startswith("//") else url for url in socials]
    return {
        "id": identity, "name": name, "address": address or None,
        "phones": unique_phones(phones),
        "socials": list(dict.fromkeys(url for url in socials if social_url(url))),
    }


def run_search(driver, city: str, query: str, count: int) -> dict:
    links = gather_links(driver, city, query, count)
    rows = []
    issues = None
    for identity, listing in links.items():
        try:
            driver.get(listing.url)
            check_page(driver, inspect_body=True)
            WebDriverWait(driver, 8).until(lambda page: page.find_elements(By.CSS_SELECTOR, CARD_DETAIL_SELECTOR))
            with suppress(ElementClickInterceptedException, StaleElementReferenceException):
                click_public_contacts(driver)
            row = parse_card(driver.page_source, identity, listing)
        except CardError as error:
            issues = {"code": error.code, "message": str(error)}
            break
        except WebDriverException:
            issues = {"code": "card_layout", "message": "Часть карточек Яндекса не удалось открыть."}
            continue
        if row:
            rows.append(row)
    if links and not rows and issues is None:
        issues = {"code": "card_layout", "message": "Разметка карточек Яндекса требует проверки."}
    return {"companies": rows, "issue": issues}


def visible_search_details(driver) -> dict:
    return driver.execute_script(r"""
        const links = [...document.querySelectorAll('a[href]')];
        const pathTypes = new Set();
        for (const link of links) {
            try {
                const url = new URL(link.href);
                if (url.pathname.startsWith('/maps/')) {
                    const segments = url.pathname.split('/').filter(Boolean);
                    pathTypes.add(url.hostname + '/' + segments.slice(0, 2).join('/'));
                }
            } catch {}
        }
        return {
            page: location.hostname + location.pathname,
            title: document.title,
            search_query: new URLSearchParams(location.search).get('text'),
            navigation_status: performance.getEntriesByType('navigation')[0]?.responseStatus ?? null,
            organization_links: links.filter((link) => link.href.includes('/org/')).length,
            search_snippets: document.querySelectorAll('[class*="search-snippet"]').length,
            map_link_types: [...pathTypes].slice(0, 10),
            visible_text: (document.body?.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 300),
        };
    """)


def main() -> int:
    arguments = argparse.ArgumentParser()
    arguments.add_argument("--browser")
    arguments.add_argument("--driver")
    arguments.add_argument("--repo", type=Path, help=argparse.SUPPRESS)  # Older diagnostic commands.
    arguments.add_argument("--city")
    arguments.add_argument("--query")
    arguments.add_argument("--limit", type=int, default=2)
    arguments.add_argument("--output", type=Path)
    arguments.add_argument("--card-url", help="Yandex Maps card URL when saved HTML lacks a canonical URL")
    mode = arguments.add_mutually_exclusive_group()
    mode.add_argument("--check-browser", action="store_true")
    mode.add_argument("--diagnose", action="store_true")
    mode.add_argument("--html", type=Path, help="parse a saved Yandex Maps card")
    options = arguments.parse_args()
    if options.html:
        identity = None
        if options.card_url:
            match = org_url(options.card_url)
            if not match:
                arguments.error("--card-url must be a Yandex Maps organization URL")
            identity = match[0]
        try:
            row = parse_card(options.html.read_bytes(), identity)
        except OSError as error:
            arguments.error(str(error))
        if not row:
            arguments.error("Не удалось найти название и ID организации в HTML карточки.")
        payload = json.dumps({"companies": [row], "issue": None}, ensure_ascii=False, indent=2)
        if options.output:
            options.output.write_text(payload + "\n", encoding="utf-8")
        else:
            print(payload)
        return 0
    if options.card_url:
        arguments.error("--card-url requires --html")
    if not options.browser:
        arguments.error("--browser is required for browser mode")
    if not options.check_browser and not all((options.city, options.query, options.output or options.diagnose)):
        arguments.error("--city, --query and either --output or --diagnose are required for a search")

    driver = None
    result = None
    try:
        driver = create_driver(options.browser, options.driver)
        driver.set_page_load_timeout(15)
        if options.check_browser:
            driver.get("about:blank")
            driver.execute_script("document.title = 'Maps browser OK'")
            return 0 if driver.title == "Maps browser OK" else 1
        result = run_search(driver, options.city, options.query, options.limit)
    except CardError as error:
        result = {"companies": [], "issue": {"code": error.code, "message": str(error)}}
    except WebDriverException as error:
        if options.check_browser:
            print("Chrome или ChromeDriver не запустился.", file=sys.stderr)
            if error.msg:
                print(error.msg.splitlines()[0], file=sys.stderr)
            return 1
        result = {"companies": [], "issue": {"code": "browser_error", "message": "Яндекс не открылся в Chrome."}}
    finally:
        if driver is not None:
            if options.diagnose and result is not None and not result["companies"]:
                if (result["issue"] or {}).get("code") != "access_restricted":
                    with suppress(WebDriverException):
                        print(json.dumps(visible_search_details(driver), ensure_ascii=False, indent=2))
            with suppress(WebDriverException):
                driver.quit()

    if options.output:
        options.output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    if options.diagnose:
        rows = result["companies"]
        print(json.dumps({
            "returned": len(rows),
            "with_address": sum(bool(row["address"]) for row in rows),
            "with_phone": sum(bool(row["phones"]) for row in rows),
            "with_social": sum(bool(row["socials"]) for row in rows),
            "issue": result["issue"],
        }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
