#!/usr/bin/env python3
"""
mangaindo_to_blogger.py
Scrape mangaindo.biz (All Mangas) -> produce Blogger Atom XML import
Usage:
    pip install -r requirements.txt
    python mangaindo_to_blogger.py --output mangaindo_blog_import.xml

Notes:
 - Test with --limit-manga N first to avoid heavy scraping.
 - Respect website rules and rate limits. Use moderate delays.
"""

import requests
from bs4 import BeautifulSoup
import time
import argparse
import uuid
from datetime import datetime
import html
import sys
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " \
             "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"

HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
BASE_URL = "https://mangaindo.biz"

session = requests.Session()
session.headers.update(HEADERS)


def safe_get(url, max_retries=3, backoff=1.0, timeout=20):
    for attempt in range(max_retries):
        try:
            r = session.get(url, timeout=timeout)
            if r.status_code == 200:
                return r
            else:
                print(f"WARN: {url} returned status {r.status_code}", file=sys.stderr)
                time.sleep(backoff * (attempt + 1))
        except Exception as e:
            print(f"ERR: {url} -> {e}", file=sys.stderr)
            time.sleep(backoff * (attempt + 1))
    return None


def parse_all_mangas_page(html_text):
    """
    Parse one 'all-mangas' page's HTML and extract manga links+title+thumb.
    This function expects the real page HTML (after JS rendered). For saved HTML,
    it may or may not include full list. We rely on server-side paged markup.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    items = []
    # Typical theme uses .bsx .bsx or .c-columns .bsx or item-thumb classes.
    # We'll try several selectors that commonly appear in Madara-based themes.
    # 1) items inside elements with class 'bsx' or 'page-item-detail' or '.bsx .thumb'
    selectors = [
        ".bsx a",                 # earlier script idea
        ".page-item-detail .item-thumb a",
        ".c-page__content .page-listing-item .page-item-detail a",
        ".post .thumb a",
        ".item-thumb a",
        ".cover a"
    ]
    seen = set()
    for sel in selectors:
        for a in soup.select(sel):
            href = a.get("href")
            if not href:
                continue
            href = urljoin(BASE_URL, href)
            if href in seen:
                continue
            seen.add(href)
            # try to get title and image inside anchor
            title = None
            image = None
            # title often in sibling or inside .post-title or .tt or .post-title
            parent = a
            # find nearby title
            t = None
            # look for common title classes
            candidates = [
                a.get("title"),
                a.get("aria-label"),
                (a.select_one(".tt") and a.select_one(".tt").get_text(strip=True)) if a.select_one(".tt") else None,
            ]
            # fallback: look at parent elements for .post-title, .title, h3, h2
            p = a
            for _ in range(4):
                if p is None:
                    break
                if t is None:
                    title_node = p.select_one(".post-title") or p.select_one(".post-title a") or p.select_one(".title") or p.select_one("h3") or p.select_one("h2")
                    if title_node:
                        t = title_node.get_text(strip=True)
                p = p.parent
            for c in candidates:
                if c:
                    title = c.strip()
                    break
            if not title and t:
                title = t.strip()
            # image
            img = a.select_one("img")
            if img:
                image = img.get("data-src") or img.get("src") or img.get("data-lazy-src")
                if image:
                    image = urljoin(BASE_URL, image)
            # as fallback, check sibling or parent for thumbnail img
            if not image:
                par = a.parent
                if par:
                    img2 = par.select_one("img")
                    if img2:
                        image = img2.get("data-src") or img2.get("src")
                        if image:
                            image = urljoin(BASE_URL, image)
            items.append({"title": title or "", "link": href, "image": image or ""})
    return items


def find_pagination_next(soup):
    # look for next page link
    next_sel = soup.select_one("a.next, a.paginate-next, li.next a, .wp-pagenavi a.next")
    if next_sel:
        return urljoin(BASE_URL, next_sel.get("href"))
    # fallback: find page links and choose next by number (not implemented)
    return None


def scrape_all_mangas(start_url, limit_manga=0, sleep_between_pages=1.0):
    """
    Follow pagination of /all-mangas/ and collect manga entries
    """
    mangas = []
    next_url = start_url
    page_count = 0
    while next_url:
        print(f"Fetching page: {next_url}", file=sys.stderr)
        r = safe_get(next_url)
        if not r:
            print(f"Failed to load page {next_url}", file=sys.stderr)
            break
        page_count += 1
        page_items = parse_all_mangas_page(r.text)
        # dedupe by link
        existing_links = {m['link'] for m in mangas}
        added = 0
        for it in page_items:
            if it['link'] not in existing_links:
                mangas.append(it)
                existing_links.add(it['link'])
                added += 1
                if limit_manga and len(mangas) >= limit_manga:
                    print(f"Reached limit {limit_manga}", file=sys.stderr)
                    return mangas
        print(f"Added {added} items from page {page_count}", file=sys.stderr)
        soup = BeautifulSoup(r.text, "html.parser")
        nxt = find_pagination_next(soup)
        if not nxt:
            # try page/2 pattern
            # if first run and no next, attempt to guess /page/2/
            if page_count == 1:
                parsed = urlparse(next_url)
                if not parsed.path.endswith("/page/1/") and not parsed.path.endswith("/page/"):
                    # try add page/2
                    nxt = urljoin(next_url.rstrip("/") + "/", "page/2/")
            if not nxt:
                break
        next_url = nxt
        time.sleep(sleep_between_pages)
    return mangas


def extract_manga_detail(manga_url):
    """
    Get manga detail page: title, description, image (if missing), and attempt to fetch chapters via ajax
    """
    r = safe_get(manga_url)
    if not r:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    # title
    title_tag = soup.select_one(".post-title h1") or soup.select_one(".post-title") or soup.select_one("h1.entry-title") or soup.select_one("h1")
    title = title_tag.get_text(strip=True) if title_tag else ""
    # description / synopsis
    desc = ""
    # common selectors
    desc_sel = soup.select_one(".summary_content, .entry-content .summary, .main-content .summary, .summary, .post .entry-content")
    if desc_sel:
        desc = desc_sel.get_text(separator="\n", strip=True)
    else:
        # fallback to meta description
        meta_desc = soup.find("meta", {"name": "description"})
        if meta_desc and meta_desc.get("content"):
            desc = meta_desc.get("content").strip()
    # image: try .summary_image img or og:image
    img = None
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        img = og.get("content")
    if not img:
        img_node = soup.select_one(".summary_image img") or soup.select_one(".post-thumb img") or soup.select_one(".entry-content img")
        if img_node:
            img = img_node.get("data-src") or img_node.get("src")
    if img:
        img = urljoin(manga_url, img)
    # chapters via ajax endpoint pattern: manga_url + "ajax/chapters/?t=1"
    chapters = []
    try:
        ajax_url = manga_url.rstrip("/") + "/ajax/chapters/?t=1"
        ar = safe_get(ajax_url)
        if ar and ar.status_code == 200:
            # response may be HTML fragment with li.wp-manga-chapter
            a_soup = BeautifulSoup(ar.text, "html.parser")
            for li in a_soup.select("li.wp-manga-chapter a"):
                ch_title = li.get_text(strip=True)
                ch_link = urljoin(ajax_url, li.get("href"))
                chapters.append({"title": ch_title, "link": ch_link})
        # fallback: find chapters in page itself
        if not chapters:
            for li in soup.select("li.wp-manga-chapter a"):
                ch_title = li.get_text(strip=True)
                ch_link = urljoin(manga_url, li.get("href"))
                chapters.append({"title": ch_title, "link": ch_link})
    except Exception as e:
        print("AJAX chapter fetch failed:", e, file=sys.stderr)

    return {"title": title, "description": desc, "image": img, "chapters": chapters, "url": manga_url}


def make_atom_entry(title, content_html, link, published=None, categories=None):
    published = published or datetime.utcnow().isoformat() + "Z"
    categories = categories or []
    # Escape title and content
    t = html.escape(title)
    cdata = f"<![CDATA[{content_html}]]>"
    cats_xml = ""
    for c in categories:
        cats_xml += f"\n    <category scheme='http://www.blogger.com/atom/ns#' term='{html.escape(c)}'/>"
    entry = f"""
  <entry>
    <title type='text'>{t}</title>
    <content type='html'>{cdata}</content>
    <published>{published}</published>
    <updated>{published}</updated>{cats_xml}
    <link rel='alternate' type='text/html' href='{html.escape(link)}'/>
  </entry>
"""
    return entry


def build_blogger_feed(manga_details_list, include_chapters=True, include_manga_post=True, blog_title="Mangaindo Import"):
    feed_id = str(uuid.uuid4())
    updated = datetime.utcnow().isoformat() + "Z"
    head = f"""<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns='http://www.w3.org/2005/Atom'
      xmlns:openSearch='http://a9.com/-/spec/opensearch/1.1/'
      xmlns:blogger='http://schemas.google.com/blogger/2008'
      xmlns:gd='http://schemas.google.com/g/2005'
      xmlns:thr='http://purl.org/syndication/thread/1.0'>
<title type='text'>{html.escape(blog_title)}</title>
<updated>{updated}</updated>
<id>urn:uuid:{feed_id}</id>
<author><name>Imported</name></author>
"""
    entries = ""
    for m in manga_details_list:
        # manga post
        if include_manga_post:
            title = m.get("title") or m.get("url")
            img_html = f"<p><img src='{html.escape(m.get('image') or '')}' alt='{html.escape(title)}'></p>" if m.get("image") else ""
            desc_html = f"<p>{html.escape(m.get('description') or '')}</p>" if m.get("description") else ""
            content_html = f"{img_html}{desc_html}<p>Source: <a href='{html.escape(m.get('url'))}'>{html.escape(m.get('url'))}</a></p>"
            entries += make_atom_entry(title, content_html, m.get("url"), categories=["Manga", "Komik"])
        # chapters
        if include_chapters and m.get("chapters"):
            for ch in m.get("chapters"):
                ch_title = f"{(m.get('title') or '')} — {ch.get('title')}"
                content_html = f"<p>Chapter link: <a href='{html.escape(ch.get('link'))}'>{html.escape(ch.get('link'))}</a></p><p>From manga: <a href='{html.escape(m.get('url'))}'>{html.escape(m.get('url'))}</a></p>"
                entries += make_atom_entry(ch_title, content_html, ch.get("link"), categories=["Chapter", "Manga"])
    foot = "</feed>"
    return head + entries + foot


def main():
    parser = argparse.ArgumentParser(description="Scrape Mangaindo and produce Blogger-compatible Atom XML")
    parser.add_argument("--start-url", default=urljoin(BASE_URL, "/all-mangas/"), help="All mangas start URL")
    parser.add_argument("--output", default="mangaindo_blogger_import.xml", help="Output XML filename")
    parser.add_argument("--limit-manga", type=int, default=0, help="Limit number of manga to scrape (0 = all)")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent workers to fetch manga details")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between page requests (seconds)")
    parser.add_argument("--no-chapters", action="store_true", help="Do not include chapter posts")
    parser.add_argument("--no-manga-posts", action="store_true", help="Do not include manga-level posts (only chapters)")
    parser.add_argument("--test", action="store_true", help="Quick test run (small limit)")
    args = parser.parse_args()

    if args.test and args.limit_manga == 0:
        args.limit_manga = 10

    print("START: scraping list pages...", file=sys.stderr)
    mangas = scrape_all_mangas(args.start_url, limit_manga=args.limit_manga, sleep_between_pages=args.delay)
    print(f"Found {len(mangas)} manga links.", file=sys.stderr)

    # fetch details
    details = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        future_to_url = {ex.submit(extract_manga_detail, m['link']): m for m in mangas}
        for fut in as_completed(future_to_url):
            m = future_to_url[fut]
            try:
                d = fut.result()
                if d:
                    # ensure at least link and title exist
                    if not d.get("title"):
                        d["title"] = m.get("title") or m.get("link")
                    if not d.get("image"):
                        d["image"] = m.get("image")
                    details.append(d)
                    print(f"DETAIL OK: {d.get('title')}", file=sys.stderr)
                else:
                    print(f"DETAIL FAIL: {m['link']}", file=sys.stderr)
            except Exception as e:
                print(f"ERROR extracting {m['link']}: {e}", file=sys.stderr)

    print("Building Blogger Atom XML...", file=sys.stderr)
    feed_xml = build_blogger_feed(details, include_chapters=not args.no_chapters, include_manga_post=not args.no_manga_posts)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(feed_xml)
    print(f"Saved {args.output} (manga: {len(details)})", file=sys.stderr)


if __name__ == "__main__":
    main()
