import os
import io
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
import httpx
import asyncio
import re
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from jinja2 import Template
import anthropic
from supabase import create_client
from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN

app = FastAPI()

# RSS 피드 목록
RSS_FEEDS = [
    {"name": "로봇신문-AI", "url": "https://www.irobotnews.com/rss/S1N2.xml"},
    {"name": "로봇신문-로봇", "url": "https://www.irobotnews.com/rss/S1N1.xml"},
    {"name": "전자신문-AI", "url": "http://rss.etnews.com/04046.xml"},
    {"name": "The AI", "url": "https://www.newstheai.com/rss/allArticle.xml"},
    {"name": "ZDNet Korea", "url": "https://zdnet.co.kr/feed"},
    {"name": "TechCrunch", "url": "https://techcrunch.com/category/artificial-intelligence/feed/"},
    {"name": "The Verge", "url": "https://www.theverge.com/rss/index.xml"},
    {"name": "Wired", "url": "https://www.wired.com/feed/category/business/latest/rss"},
    {"name": "OpenAI", "url": "https://openai.com/news/rss.xml"},
    {"name": "AI Jobs", "url": "https://aijobs.net/feed/"},
    {"name": "AI (arxiv)", "url": "http://export.arxiv.org/rss/cs.AI"},
    {"name": "Techmeme", "url": "https://www.techmeme.com/feed.xml"},
    {"name": "Hugging Face", "url": "https://huggingface.co/blog/feed.xml"},
]

# 활성화된 피드 인덱스 (로봇신문-AI, 로봇신문-로봇, 전자신문-AI, ZDNet Korea, Techmeme, Hugging Face)
ACTIVE_FEED_INDICES = {0, 1, 2, 4, 11, 12}

# Configure Claude API using environment variable
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

if ANTHROPIC_API_KEY:
    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
else:
    print("Warning: ANTHROPIC_API_KEY environment variable is not set.")
    client = None

# Configure Supabase
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

supabase = None
supabase_error = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        supabase_error = f"Supabase 연결 실패: {e}"
        print(f"Warning: {supabase_error}")
else:
    supabase_error = "SUPABASE_URL 또는 SUPABASE_KEY 환경변수가 설정되지 않았습니다."
    print(f"Warning: {supabase_error}")


def save_articles_to_db(articles, publisher=""):
    """뉴스 기사를 Supabase에 저장. publisher와 published_at 포함"""
    global supabase_error
    supabase_error = None
    if not supabase:
        return
    for article in articles:
        try:
            row = {
                "title": article["title"],
                "content": article["body"],
                "summary": article.get("summary", ""),
                "url": article["link"],
                "publisher": publisher or article.get("publisher", ""),
            }
            if article.get("pub_date"):
                row["published_at"] = article["pub_date"]
            supabase.table("news-understanding").upsert(
                row,
                on_conflict="url",
            ).execute()
        except Exception as e:
            supabase_error = f"DB 저장 오류: {e}"
            print(supabase_error)


def load_articles_by_publisher(publisher):
    """Supabase에서 특정 제공자의 기사 목록 조회"""
    global supabase_error
    supabase_error = None
    if not supabase:
        return []
    try:
        result = (
            supabase.table("news-understanding")
            .select("*")
            .eq("publisher", publisher)
            .order("created_at", desc=True)
            .limit(50)
            .execute()
        )
        return [
            {
                "title": row["title"],
                "body": row["content"],
                "summary": row.get("summary", ""),
                "link": row["url"],
                "pub_date": row.get("published_at", ""),
                "publisher": row.get("publisher", ""),
            }
            for row in result.data
        ]
    except Exception as e:
        supabase_error = f"DB 조회 오류: {e}"
        print(supabase_error)
        return []


def update_article_summary(url, summary):
    """기사 URL로 summary 컬럼만 업데이트"""
    if not supabase:
        return False
    try:
        supabase.table("news-understanding").update(
            {"summary": summary}
        ).eq("url", url).execute()
        return True
    except Exception as e:
        print(f"Summary 업데이트 오류: {e}")
        return False


def get_news_stats():
    """DB에 저장된 뉴스 통계 조회 (제공자별 기사 수)"""
    if not supabase:
        return {}
    try:
        result = (
            supabase.table("news-understanding")
            .select("publisher")
            .execute()
        )
        stats = {}
        for row in result.data:
            pub = row.get("publisher", "기타")
            stats[pub] = stats.get(pub, 0) + 1
        return stats
    except Exception as e:
        print(f"뉴스 통계 조회 오류: {e}")
        return {}

async def summarize_article(title, body):
    if not ANTHROPIC_API_KEY or not client:
        return "⚠️ API 키가 설정되지 않았습니다. Render.com 설정에서 ANTHROPIC_API_KEY를 추가해주세요."

    if not body or body == "Content not found" or len(body) < 100:
        return "요약할 충분한 본문 내용이 없습니다."

    prompt = f"""다음 뉴스 기사를 읽고 아래 형식에 정확히 맞춰 요약해 주세요.

형식:
[기사 제목 1줄]
. 핵심 요약 첫 번째 줄 (2줄 이내)
. 핵심 요약 두 번째 줄 (2줄 이내)

주의사항:
- 반드시 '.'으로 시작하는 2개의 문장으로 요약하세요.
- 불필요한 설명 없이 핵심만 전달하세요.
- 마크다운 문법(**, ##, *, # 등)을 절대 사용하지 마세요. 순수 텍스트로만 작성하세요.

기사 제목: {title}
기사 본문: {body}"""

    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system="당신은 뉴스 요약 전문가입니다.",
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text.strip()
        text = re.sub(r'\*+', '', text)
        text = re.sub(r'#+\s*', '', text)
        text = re.sub(r'`+', '', text)
        text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
        return text.strip()
    except Exception as e:
        return f"요약 중 오류가 발생했습니다: {str(e)}"

async def get_news_content(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
    }
    error_reason = ""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as h_client:
            response = await h_client.get(url, headers=headers)
            status_code = response.status_code
            html_text = response.text
            if status_code >= 400:
                # 403/401이라도 HTML에 meta 태그가 있을 수 있으므로 파싱 시도
                soup_err = BeautifulSoup(html_text, 'html.parser')
                og_t = soup_err.find('meta', property='og:title')
                og_d = soup_err.find('meta', property='og:description')
                err_title = og_t['content'].strip() if og_t and og_t.get('content') else ""
                err_desc = og_d['content'].strip() if og_d and og_d.get('content') else ""
                if err_title and err_desc:
                    reason = "[페이월/접근 제한] 전체 본문을 가져올 수 없어 요약 정보만 표시합니다."
                    return err_title, reason + "\n\n" + err_desc, ""
                if status_code == 403:
                    return "추출 실패", "[추출 실패] HTTP 403 Forbidden — 이 사이트는 봇 접근을 차단하고 있습니다. (페이월 또는 봇 방지)", ""
                elif status_code == 401:
                    return "추출 실패", "[추출 실패] HTTP 401 Unauthorized — 로그인이 필요한 페이지입니다.", ""
                else:
                    return "추출 실패", f"[추출 실패] HTTP {status_code} — 서버에서 요청을 거부했습니다.", ""

        # JavaScript 렌더링 전용 페이지 감지
        if len(html_text.strip()) < 500 and ('javascript' in html_text.lower() or 'noscript' in html_text.lower()):
            error_reason = "[추출 실패] 이 페이지는 JavaScript로 렌더링되어 서버에서 직접 추출할 수 없습니다."
            return "추출 실패", error_reason, ""

        soup = BeautifulSoup(html_text, 'html.parser')

        # --- 제목 추출 (meta 태그 우선) ---
        title = ""
        og_title = soup.find('meta', property='og:title')
        if og_title and og_title.get('content', '').strip():
            title = og_title['content'].strip()
        else:
            twitter_title = soup.find('meta', attrs={'name': 'twitter:title'})
            if twitter_title and twitter_title.get('content', '').strip():
                title = twitter_title['content'].strip()

        if not title:
            title_selectors = ['h2.title', 'h2.title_news', 'div.article_title h2', 'h1.article-title', 'h1.headline', 'h1[data-testid]', 'h1', 'h2']
            for selector in title_selectors:
                el = soup.select_one(selector)
                if el and el.get_text(strip=True):
                    title = el.get_text(strip=True)
                    break

        if not title:
            title_tag = soup.find('title')
            title = title_tag.get_text(strip=True) if title_tag else "제목을 찾을 수 없음"

        # --- 날짜 추출 (meta 태그) ---
        pub_date = ""
        for meta_prop in ['article:published_time', 'og:article:published_time', 'datePublished']:
            meta_el = soup.find('meta', property=meta_prop) or soup.find('meta', attrs={'name': meta_prop})
            if meta_el and meta_el.get('content', '').strip():
                pub_date = meta_el['content'].strip()
                break
        if not pub_date:
            time_el = soup.find('time', attrs={'datetime': True})
            if time_el:
                pub_date = time_el['datetime'].strip()

        # --- 본문 추출 ---
        body_selectors = [
            # 한국 뉴스 사이트
            'div.article_txt', 'div.article_body', 'div#articleBody',
            'div#article-view-content-div', 'div.news_cnt_detail_wrap',
            # 국제 뉴스 사이트
            '[itemprop="articleBody"]', 'div.article-body', 'div.article__body',
            'div.story-body', 'div.article-content', 'div.post-content',
            'div.body-content', 'section.article-body',
            'div[data-component="text-block"]',
            # 대학/기관 사이트
            '#center', 'div.field-item', 'div.node-content',
            # 블로그/일반
            'div.entry-content', 'div.blog-content', 'main article',
            'article', 'div.content', 'main',
        ]
        body_element = None
        for selector in body_selectors:
            el = soup.select_one(selector)
            if el:
                # script, style, nav, footer, aside 태그 제거
                for tag in el.find_all(['script', 'style', 'nav', 'footer', 'aside', 'iframe', 'header']):
                    tag.decompose()
                if len(el.get_text(strip=True)) > 100:
                    body_element = el
                    break

        if body_element:
            body = body_element.get_text(separator='\n', strip=True)
        else:
            # <p> 태그에서 본문 수집 (셀렉터 매칭 실패 시)
            body = ""
            paragraphs = soup.find_all('p')
            p_texts = [p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 40]
            if p_texts:
                body = '\n'.join(p_texts)

            # meta description 폴백 (p 태그도 부족한 경우 보충)
            if len(body) < 100:
                meta_body = ""
                og_desc = soup.find('meta', property='og:description')
                if og_desc and og_desc.get('content', '').strip():
                    meta_body = og_desc['content'].strip()
                else:
                    meta_desc = soup.find('meta', attrs={'name': 'description'})
                    if meta_desc and meta_desc.get('content', '').strip():
                        meta_body = meta_desc['content'].strip()
                if meta_body:
                    body = meta_body + ("\n\n" + body if body else "")

            if not body:
                error_reason = "[추출 실패] 이 페이지에서 뉴스 본문 영역을 찾을 수 없습니다. 페이월, JavaScript 렌더링, 또는 비표준 HTML 구조일 수 있습니다."
                return title or "추출 실패", error_reason, pub_date

        # 텍스트 정제
        body = re.sub(r'(?:이메일|email|e-mail)\s*[:\s]*\s*[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', '', body, flags=re.IGNORECASE)
        body = re.sub(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', '', body)
        body = re.sub(r'[가-힣]{2,4}\s*기자(?!\w)', '', body)
        body = re.sub(r'\[\s*\]|\(\s*\)', '', body)
        body = '\n'.join([line.strip() for line in body.split('\n') if line.strip()])

        if len(body) < 50:
            # meta description이라도 있으면 그것을 보여줌
            og_desc = soup.find('meta', property='og:description')
            meta_desc_tag = soup.find('meta', attrs={'name': 'description'})
            fallback = ""
            if og_desc and og_desc.get('content', '').strip():
                fallback = og_desc['content'].strip()
            elif meta_desc_tag and meta_desc_tag.get('content', '').strip():
                fallback = meta_desc_tag['content'].strip()
            if fallback:
                return title or "추출 실패", "[페이월/접근 제한] 전체 본문을 가져올 수 없어 요약 정보만 표시합니다.\n\n" + fallback, pub_date
            error_reason = f"[추출 실패] 본문이 너무 짧습니다 ({len(body)}자). 페이월이 있거나 JavaScript로 렌더링되는 페이지일 수 있습니다."
            return title or "추출 실패", error_reason, pub_date

        return title, body, pub_date
    except httpx.TimeoutException:
        return "추출 실패", "[추출 실패] 요청 시간이 초과되었습니다 (15초). 서버가 응답하지 않거나 봇 접근을 차단하고 있을 수 있습니다.", ""
    except httpx.ConnectError:
        return "추출 실패", f"[추출 실패] 서버에 연결할 수 없습니다. URL을 확인해주세요: {url}", ""
    except Exception as e:
        return "추출 실패", f"[추출 실패] {type(e).__name__}: {e}", ""

async def parse_rss_and_fetch_news(rss_url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    articles = []
    try:
        async with httpx.AsyncClient(follow_redirects=True) as h_client:
            response = await h_client.get(rss_url, headers=headers)
            response.raise_for_status()
            content = response.text
            
        root = ET.fromstring(content)
        items = []
        for item in root.findall('.//item'):
            link_elem = item.find('link')
            if link_elem is not None and link_elem.text:
                title_elem = item.find('title')
                desc_elem = item.find('description')
                rss_title = title_elem.text.strip() if title_elem is not None and title_elem.text else ""
                rss_desc = desc_elem.text.strip() if desc_elem is not None and desc_elem.text else ""
                pub_date_elem = item.find('pubDate')
                rss_pub_date = pub_date_elem.text.strip() if pub_date_elem is not None and pub_date_elem.text else ""
                # Techmeme 등 aggregator: description에서 원본 기사 URL 추출
                original_url = None
                if rss_desc and 'techmeme.com' in (link_elem.text or ''):
                    from bs4 import BeautifulSoup as _BS
                    desc_soup = _BS(rss_desc, 'html.parser')
                    # 본문 큰 링크(SPAN > B > A)에서 원본 URL 추출
                    span = desc_soup.find('span')
                    if span:
                        a_tag = span.find('a', href=True)
                        if a_tag and 'techmeme.com' not in a_tag['href']:
                            original_url = a_tag['href']
                    # description에서 텍스트만 추출하여 요약으로 사용
                    rss_desc = desc_soup.get_text(separator=' ', strip=True)
                items.append({
                    'link': original_url or link_elem.text,
                    'rss_title': rss_title,
                    'rss_desc': rss_desc,
                    'rss_pub_date': rss_pub_date,
                    'original_url': original_url,
                })

        fetch_tasks = [get_news_content(it['link']) for it in items[:10]]
        fetched_results = await asyncio.gather(*fetch_tasks)

        for (title, body, page_date), it in zip(fetched_results, items[:10]):
            # 페이지 접근 실패 시 RSS 데이터로 대체
            if title.startswith("오류 발생:") or not body or body == "본문을 찾을 수 없습니다." or body.startswith("[추출 실패]") or body.startswith("[페이월"):
                title = it['rss_title'] or title
                body = it['rss_desc'] or body
            # RSS pubDate 우선, 없으면 페이지에서 추출한 날짜 사용
            pub_date = it.get('rss_pub_date', '') or page_date
            articles.append({
                'title': title,
                'body': body,
                'link': it['link'],
                'summary': '',
                'pub_date': pub_date
            })
    except Exception as e:
        raise Exception(f"RSS 파싱 중 오류 발생: {e}")
    return articles

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>뉴스 요약기</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: 'Pretendard', sans-serif; background-color: #f0f2f5; display: flex; min-height: 100vh; }

        /* 왼쪽 사이드바 */
        .sidebar {
            width: 200px;
            min-width: 200px;
            background: #1a2233;
            padding: 20px 0;
            display: flex;
            flex-direction: column;
            position: fixed;
            top: 0;
            left: 0;
            height: 100vh;
            overflow-y: auto;
            z-index: 100;
        }
        .sidebar-title {
            color: #fff;
            font-size: 16px;
            font-weight: bold;
            text-align: center;
            padding: 10px 15px 20px;
            border-bottom: 1px solid #2a3a50;
            margin-bottom: 10px;
        }
        .feed-tab {
            display: block;
            width: 100%;
            padding: 14px 18px;
            background: none;
            border: none;
            color: #a0b0c8;
            font-size: 14px;
            text-align: left;
            cursor: pointer;
            transition: all 0.2s;
            border-left: 3px solid transparent;
            text-decoration: none;
        }
        .feed-tab:hover {
            background: #253045;
            color: #fff;
        }
        .feed-tab.active {
            background: #253045;
            color: #4a9eff;
            border-left-color: #4a9eff;
            font-weight: bold;
        }

        /* 오른쪽 콘텐츠 영역 */
        .content {
            margin-left: 200px;
            flex: 1;
            padding: 25px 30px;
        }
        .content-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 25px;
        }
        .content-header h1 {
            color: #1a73e8;
            font-size: 24px;
        }
        .ppt-btn {
            display: inline-block;
            padding: 12px 24px;
            background-color: #e67e22;
            color: white;
            text-decoration: none;
            border-radius: 8px;
            font-size: 15px;
            font-weight: bold;
            transition: background 0.3s;
        }
        .ppt-btn:hover { background-color: #cf6d17; }

        .article-date { font-size: 12px; color: #888; background: #f5f6f8; padding: 3px 10px; border-radius: 4px; white-space: nowrap; flex-shrink: 0; }
        .result-item { background: white; margin-bottom: 30px; padding: 25px; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.07); }
        .result-item h2 { margin-top: 0; color: #333; font-size: 22px; border-bottom: 2px solid #f0f2f5; padding-bottom: 12px; }
        .result-item h2 a { text-decoration: none; color: inherit; }

        .article-layout { display: flex; gap: 25px; margin-top: 15px; align-items: flex-start; }
        .article-body { flex: 0 0 auto; width: 50%; font-size: 15px; line-height: 1.8; color: #444; max-height: 500px; overflow-y: auto; padding-right: 15px; }

        .summary-section { flex: 0 0 800px; background-color: #fff9db; border: 2px solid #fab005; border-radius: 12px; padding: 20px; position: sticky; top: 20px; display: none; }
        .summary-section.visible { display: block; }
        .summary-section h3 { margin-top: 0; color: #e67e22; font-size: 18px; display: flex; align-items: center; gap: 8px; border-bottom: 1px solid #ffe066; padding-bottom: 10px; }
        .summary-content { font-size: 16px; color: #2c3e50; font-weight: 600; line-height: 1.6; white-space: pre-wrap; }

        .btn-row { display: flex; gap: 10px; margin-top: 20px; align-items: center; }
        .original-btn { display: inline-block; padding: 8px 18px; border: 1px solid #1a73e8; color: #1a73e8; text-decoration: none; border-radius: 6px; font-size: 13px; transition: all 0.3s; }
        .original-btn:hover { background-color: #1a73e8; color: white; }
        .summarize-btn { display: inline-block; padding: 8px 18px; background-color: #fab005; color: #fff; border: none; border-radius: 6px; font-size: 13px; font-weight: bold; cursor: pointer; transition: all 0.3s; }
        .summarize-btn:hover { background-color: #e6a200; }
        .summarize-btn:disabled { background-color: #ccc; cursor: not-allowed; }

        .error-msg { background: #fff5f5; color: #c92a2a; padding: 15px; border-radius: 8px; border: 1px solid #ffc9c9; margin-bottom: 20px; }
        .result-item.extraction-error { border-left: 4px solid #e03131; background: #fff8f8; }
        .result-item.extraction-error h2 a { color: #c92a2a; }
        .result-item.extraction-partial { border-left: 4px solid #f59f00; background: #fffdf5; }
        .error-detail { color: #c92a2a; font-size: 14px; padding: 12px 16px; background: #fff0f0; border-radius: 6px; border: 1px solid #ffc9c9; white-space: pre-wrap; line-height: 1.6; }
        .paywall-notice { color: #e67700; font-size: 13px; padding: 8px 12px; background: #fff9e6; border-radius: 6px; border: 1px solid #ffe066; margin-bottom: 8px; }
        .loading-overlay { display: none; text-align: center; color: #1a73e8; font-weight: bold; font-size: 18px; padding: 40px 0; }
        .db-error { font-size: 13px; color: #c92a2a; background: #fff5f5; border: 1px solid #ffc9c9; border-radius: 8px; padding: 8px 15px; margin-bottom: 15px; }
        .empty-state { text-align: center; color: #888; padding: 60px 20px; font-size: 18px; }

        .home-tab { border-bottom: 1px solid #2a3a50; margin-bottom: 5px; font-weight: 600; }
        .home-screen { text-align: center; padding: 80px 20px 40px; }
        .home-icon { font-size: 64px; margin-bottom: 20px; }
        .home-title { font-size: 32px; color: #1a73e8; margin-bottom: 12px; font-weight: 700; }
        .home-desc { font-size: 18px; color: #666; margin-bottom: 50px; }
        .home-feeds { display: flex; flex-wrap: wrap; gap: 14px; justify-content: center; max-width: 800px; margin: 0 auto 40px; }
        .home-feed-card {
            padding: 14px 24px; background: #fff; border: 1px solid #e0e0e0; border-radius: 10px;
            color: #333; text-decoration: none; font-size: 15px; font-weight: 500;
            transition: all 0.2s; box-shadow: 0 1px 4px rgba(0,0,0,0.06);
        }
        .home-feed-card:hover { border-color: #1a73e8; color: #1a73e8; box-shadow: 0 3px 12px rgba(26,115,232,0.15); transform: translateY(-2px); }
        .home-hint { font-size: 14px; color: #aaa; }

        .daily-btn {
            flex-shrink: 0;
            padding: 6px 14px;
            background-color: #27ae60;
            color: #fff;
            border: none;
            border-radius: 6px;
            font-size: 12px;
            font-weight: bold;
            cursor: pointer;
            transition: all 0.3s;
            white-space: nowrap;
        }
        .daily-btn:hover { background-color: #219a52; }
        .daily-btn:disabled { background-color: #95a5a6; cursor: not-allowed; }
        .daily-btn.selected { background-color: #95a5a6; }

        .daily-item {
            background: white; margin-bottom: 20px; padding: 20px 25px; border-radius: 12px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.07); position: relative;
        }
        .daily-item h3 { color: #333; font-size: 18px; margin-bottom: 8px; padding-right: 80px; }
        .daily-item h3 a { text-decoration: none; color: inherit; }
        .daily-item .daily-summary { font-size: 15px; color: #2c3e50; line-height: 1.7; white-space: pre-wrap; }
        .daily-item .daily-remove {
            position: absolute; top: 15px; right: 15px;
            background: #e74c3c; color: #fff; border: none; border-radius: 50%;
            width: 28px; height: 28px; font-size: 16px; cursor: pointer; line-height: 28px; text-align: center;
        }
        .daily-item .daily-order {
            display: inline-block; background: #1a73e8; color: #fff; border-radius: 50%;
            width: 24px; height: 24px; text-align: center; line-height: 24px; font-size: 13px;
            font-weight: bold; margin-right: 8px;
        }
        .daily-source {
            display: inline-block; background: #e8f0fe; color: #1a73e8; font-size: 12px;
            font-weight: 600; padding: 3px 10px; border-radius: 12px; margin-bottom: 8px;
        }

        @media (max-width: 900px) {
            .sidebar { width: 60px; min-width: 60px; }
            .sidebar-title { font-size: 12px; padding: 10px 5px 15px; }
            .feed-tab { font-size: 11px; padding: 10px 8px; }
            .content { margin-left: 60px; padding: 15px; }
            .article-layout { flex-direction: column; }
            .article-layout { flex-direction: column; }
            .article-body { width: 100%; border-bottom: 1px solid #eee; padding-bottom: 15px; padding-right: 0; }
            .summary-section { position: static; flex: 0 0 100%; }
        }
    </style>
</head>
<body>
    <!-- 왼쪽 사이드바: 뉴스 제공자 탭 -->
    <nav class="sidebar">
        <div class="sidebar-title">뉴스 제공자</div>
        <a href="javascript:void(0)"
           class="feed-tab home-tab {% if active_feed is none %}active{% endif %}">
            Home
        </a>
        {% for feed in feeds %}
        {% if loop.index0 in active_indices %}
        <a href="javascript:void(0)"
           class="feed-tab {% if active_feed == loop.index0 %}active{% endif %}"
           data-feed-idx="{{ loop.index0 }}"
           data-feed-name="{{ feed.name }}"
           onclick="loadFeedFromDB({{ loop.index0 }}, '{{ feed.name }}')">
            {{ feed.name }}
        </a>
        {% else %}
        <a href="javascript:void(0)"
           class="feed-tab disabled-tab"
           style="color:#555;cursor:not-allowed;opacity:0.5;"
           title="준비 중">
            {{ feed.name }}
        </a>
        {% endif %}
        {% endfor %}
        <a href="javascript:void(0)"
           class="feed-tab"
           onclick="showCustomInput()"
           data-feed-idx="custom">
            직접 입력
        </a>
    </nav>

    <!-- 오른쪽 콘텐츠 영역 -->
    <main class="content">
        {% if db_error %}
        <div class="db-error">Supabase 오류: {{ db_error }}</div>
        {% endif %}

        <!-- 뉴스 수집 상태 메시지 -->
        <div id="collect-status" style="text-align:center;padding:60px 20px;display:none;">
            <div style="font-size:48px;margin-bottom:20px;">📡</div>
            <h2 id="collect-status-text" style="color:#1a73e8;font-size:22px;margin-bottom:10px;">뉴스 정보를 수집 중입니다...</h2>
            <p id="collect-status-sub" style="color:#888;font-size:15px;">잠시만 기다려주세요</p>
        </div>

        <!-- 피드 콘텐츠 헤더 -->
        <div class="content-header" id="feed-header" style="display:none;">
            <h1 id="feed-header-title"></h1>
        </div>

        <div id="loading" class="loading-overlay">뉴스를 불러오는 중입니다...</div>

        <!-- 피드 기사 목록 (JS로 렌더링) -->
        <div id="feed-articles"></div>

        <!-- 직접 입력 섹션 -->
        <div id="custom-section" style="display:none;">
            <div class="content-header">
                <h1>직접 입력</h1>
            </div>
            <div style="padding:20px 0;">
                <p style="color:#888;font-size:14px;margin-bottom:12px;">뉴스 URL을 한 줄에 하나씩 입력하세요 (최대 20개)</p>
                <textarea id="custom-urls" rows="6" style="width:100%;padding:12px;border:1px solid #ddd;border-radius:8px;font-size:14px;resize:vertical;box-sizing:border-box;" placeholder="https://example.com/news/article1&#10;https://example.com/news/article2"></textarea>
                <div style="margin-top:12px;display:flex;gap:10px;">
                    <button class="summarize-btn" onclick="fetchCustomUrls()" id="custom-fetch-btn" style="padding:12px 24px;font-size:15px;">기사 가져오기</button>
                </div>
            </div>
            <div id="custom-articles"></div>
        </div>

        <!-- Home / Daily News -->
        <div id="home-section">
            <div class="content-header">
                <h1>Daily News</h1>
                <div style="display:flex;gap:10px;">
                    <button class="summarize-btn" onclick="collectNews()" id="collect-btn" style="padding:12px 24px;font-size:15px;">뉴스 업데이트</button>
                    <button class="ppt-btn" onclick="downloadDailyPPT()" id="daily-ppt-btn" style="display:none;">PPT 다운로드</button>
                    <button onclick="clearDaily()" id="daily-clear-btn" style="display:none;padding:12px 24px;background:#e74c3c;color:#fff;border:none;border-radius:8px;font-size:15px;font-weight:bold;cursor:pointer;">전체 삭제</button>
                </div>
            </div>
            <div id="daily-empty" class="home-screen" style="padding-top:40px;">
                <div class="home-icon">📰</div>
                <h2 class="home-title">Daily News가 비어 있습니다</h2>
                <p class="home-desc">왼쪽 메뉴에서 뉴스 제공자를 선택한 후<br>"Daily News 로 선택" 버튼을 눌러 기사를 추가하세요</p>
            </div>
            <div id="daily-list"></div>
        </div>
    </main>

    <script>
    function formatPubDate(raw) {
        if (!raw) return '';
        try {
            const d = new Date(raw);
            if (isNaN(d.getTime())) return raw;
            const y = d.getFullYear();
            const m = String(d.getMonth() + 1).padStart(2, '0');
            const day = String(d.getDate()).padStart(2, '0');
            const h = String(d.getHours()).padStart(2, '0');
            const min = String(d.getMinutes()).padStart(2, '0');
            return y + '.' + m + '.' + day + ' ' + h + ':' + min;
        } catch { return raw; }
    }

    // 현재 로드된 기사 배열 (피드별로 갱신됨)
    let articles = [];
    let currentFeedName = '';
    let newsCollected = true;

    // 이스케이프 함수
    function escapeHtml(str) {
        if (!str) return '';
        return str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }

    // Daily News localStorage 관리
    function getDailyNews() {
        try { return JSON.parse(localStorage.getItem('dailyNews') || '[]'); }
        catch { return []; }
    }
    function saveDailyNews(list) {
        localStorage.setItem('dailyNews', JSON.stringify(list));
    }

    // 섹션 표시 관리
    function showSection(section) {
        document.getElementById('collect-status').style.display = 'none';
        document.getElementById('feed-header').style.display = 'none';
        document.getElementById('feed-articles').innerHTML = '';
        document.getElementById('home-section').style.display = 'none';
        document.getElementById('custom-section').style.display = 'none';
        document.getElementById('loading').style.display = 'none';

        if (section === 'home') {
            document.getElementById('home-section').style.display = '';
        } else if (section === 'collect') {
            document.getElementById('collect-status').style.display = '';
        } else if (section === 'feed') {
            document.getElementById('feed-header').style.display = '';
        } else if (section === 'custom') {
            document.getElementById('custom-section').style.display = '';
        }
    }

    // 사이드바 active 상태 업데이트
    function setActiveTab(feedIdx) {
        document.querySelectorAll('.feed-tab').forEach(tab => tab.classList.remove('active'));
        if (feedIdx === null) {
            document.querySelector('.feed-tab.home-tab').classList.add('active');
        } else {
            const tab = document.querySelector('.feed-tab[data-feed-idx="' + feedIdx + '"]');
            if (tab) tab.classList.add('active');
        }
    }

    // 직접 입력 탭 클릭
    function showCustomInput() {
        setActiveTab('custom');
        currentFeedName = '직접 입력';
        showSection('custom');
    }

    // 직접 입력: URL에서 기사 가져오기
    async function fetchCustomUrls() {
        const textarea = document.getElementById('custom-urls');
        const btn = document.getElementById('custom-fetch-btn');
        const urls = textarea.value.trim().split(String.fromCharCode(10)).map(u => u.trim()).filter(u => u.length > 0);

        if (urls.length === 0) {
            alert('URL을 입력해주세요.');
            return;
        }

        btn.disabled = true;
        btn.textContent = '가져오는 중...';
        document.getElementById('custom-articles').innerHTML = '<div class="loading-overlay" style="display:block;">기사를 가져오는 중입니다...</div>';

        try {
            const res = await fetch('/api/fetch-urls', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ urls: urls })
            });
            const data = await res.json();
            articles = (data.articles || []).map(a => ({
                title: a.title,
                body: a.body,
                link: a.link,
                summary: '',
                pub_date: a.pub_date || '',
                publisher: '직접 입력',
                error: a.error,
                paywall: a.paywall
            }));
            // DB에 저장
            if (articles.length > 0) {
                const validArticles = articles.filter(a => !a.error);
                if (validArticles.length > 0) {
                    await fetch('/api/save-custom-articles', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ articles: validArticles, publisher: '직접 입력' })
                    });
                }
            }
            renderCustomArticles();
        } catch (e) {
            document.getElementById('custom-articles').innerHTML = '<div class="error-msg">기사를 가져오는 중 오류가 발생했습니다.</div>';
        }
        btn.disabled = false;
        btn.textContent = '기사 가져오기';
    }

    // 직접 입력 기사 렌더링 (피드 기사와 동일한 형태)
    function renderCustomArticles() {
        const container = document.getElementById('custom-articles');
        if (articles.length === 0) {
            container.innerHTML = '<div class="empty-state">가져온 기사가 없습니다.</div>';
            return;
        }

        const daily = getDailyNews();
        const dailyLinks = daily.map(d => d.link);

        container.innerHTML = articles.map((a, i) => {
            if (a.error) {
                return '<div class="result-item" style="opacity:0.6;">' +
                    '<h2><a href="' + escapeHtml(a.link) + '" target="_blank">' + escapeHtml(a.link) + '</a></h2>' +
                    '<p style="color:#e74c3c;">' + escapeHtml(a.body) + '</p>' +
                '</div>';
            }
            const isSelected = dailyLinks.includes(a.link);
            const hasSummary = a.summary && a.summary.trim().length > 0;
            const dateFormatted = formatPubDate(a.pub_date);
            return '<div class="result-item" id="article-' + i + '">' +
                '<h2 style="display:flex;align-items:center;justify-content:space-between;gap:12px;">' +
                    '<a href="' + escapeHtml(a.link) + '" target="_blank" style="flex:1;">' + escapeHtml(a.title) + '</a>' +
                    (dateFormatted ? '<span class="article-date">' + dateFormatted + '</span>' : '') +
                    '<button class="daily-btn' + (isSelected ? ' selected' : '') + '" onclick="selectForDaily(' + i + ')" id="daily-btn-' + i + '"' + (isSelected ? ' disabled' : '') + '>' + (isSelected ? '선택됨' : 'Daily News 로 선택') + '</button>' +
                '</h2>' +
                '<div class="article-layout">' +
                    '<div class="article-body">' + escapeHtml(a.body) + '</div>' +
                    '<div class="summary-section' + (hasSummary ? ' visible' : '') + '" id="summary-' + i + '">' +
                        '<h3>요약</h3>' +
                        '<div class="summary-content">' + (hasSummary ? escapeHtml(a.summary) : '') + '</div>' +
                    '</div>' +
                '</div>' +
                '<div class="btn-row">' +
                    '<a href="' + escapeHtml(a.link) + '" target="_blank" class="original-btn">원문 보기</a>' +
                    '<button class="summarize-btn" onclick="summarizeArticle(' + i + ')"' + (hasSummary ? ' disabled' : '') + '>' + (hasSummary ? '요약 완료' : '요약하기') + '</button>' +
                '</div>' +
            '</div>';
        }).join('');
    }

    // DB에서 기사 로드 후 렌더링
    async function loadFeedFromDB(feedIdx, feedName) {
        setActiveTab(feedIdx);
        currentFeedName = feedName;
        showSection('feed');
        document.getElementById('feed-header-title').textContent = feedName;
        document.getElementById('loading').style.display = 'block';

        try {
            const res = await fetch('/api/articles?publisher=' + encodeURIComponent(feedName));
            const data = await res.json();
            articles = data.articles || [];
            renderFeedArticles();
        } catch (e) {
            document.getElementById('feed-articles').innerHTML = '<div class="error-msg">기사를 불러오는 중 오류가 발생했습니다.</div>';
        }
        document.getElementById('loading').style.display = 'none';
    }

    // 피드 기사 렌더링
    function renderFeedArticles() {
        const container = document.getElementById('feed-articles');
        if (articles.length === 0) {
            container.innerHTML = '<div class="empty-state">이 제공자의 뉴스가 없습니다.</div>';
            return;
        }

        const daily = getDailyNews();
        const dailyLinks = daily.map(d => d.link);

        container.innerHTML = articles.map((a, i) => {
            const isSelected = dailyLinks.includes(a.link);
            const hasSummary = a.summary && a.summary.trim().length > 0;
            const dateFormatted = formatPubDate(a.pub_date);
            return '<div class="result-item" id="article-' + i + '">' +
                '<h2 style="display:flex;align-items:center;justify-content:space-between;gap:12px;">' +
                    '<a href="' + escapeHtml(a.link) + '" target="_blank" style="flex:1;">' + escapeHtml(a.title) + '</a>' +
                    (dateFormatted ? '<span class="article-date">' + dateFormatted + '</span>' : '') +
                    '<button class="daily-btn' + (isSelected ? ' selected' : '') + '" onclick="selectForDaily(' + i + ')" id="daily-btn-' + i + '"' + (isSelected ? ' disabled' : '') + '>' + (isSelected ? '선택됨' : 'Daily News 로 선택') + '</button>' +
                '</h2>' +
                '<div class="article-layout">' +
                    '<div class="article-body">' + escapeHtml(a.body) + '</div>' +
                    '<div class="summary-section' + (hasSummary ? ' visible' : '') + '" id="summary-' + i + '">' +
                        '<h3>요약</h3>' +
                        '<div class="summary-content">' + (hasSummary ? escapeHtml(a.summary) : '') + '</div>' +
                    '</div>' +
                '</div>' +
                '<div class="btn-row">' +
                    '<a href="' + escapeHtml(a.link) + '" target="_blank" class="original-btn">원문 보기</a>' +
                    '<button class="summarize-btn" onclick="summarizeArticle(' + i + ')"' + (hasSummary ? ' disabled' : '') + '>' + (hasSummary ? '요약 완료' : '요약하기') + '</button>' +
                '</div>' +
            '</div>';
        }).join('');
    }

    // Daily News 로 선택 버튼 클릭
    async function selectForDaily(idx) {
        const btn = document.getElementById('daily-btn-' + idx);
        const article = articles[idx];
        const summaryDiv = document.getElementById('summary-' + idx);
        const contentDiv = summaryDiv.querySelector('.summary-content');

        // DB에서 로드된 요약 또는 화면에 표시된 요약 확인
        const existingSummary = (article.summary && article.summary.trim().length > 0) ? article.summary :
            (summaryDiv.classList.contains('visible') ? contentDiv.textContent : null);

        if (existingSummary && existingSummary !== 'AI가 요약하는 중입니다...' && existingSummary !== '요약 중 오류가 발생했습니다.') {
            btn.disabled = true;
            const daily = getDailyNews();
            daily.push({ title: article.title, link: article.link, summary: existingSummary, source: currentFeedName });
            saveDailyNews(daily);
            btn.textContent = '선택됨';
            btn.classList.add('selected');
            return;
        }

        btn.disabled = true;
        btn.textContent = '요약 중...';

        summaryDiv.classList.add('visible');
        contentDiv.textContent = 'AI가 요약하는 중입니다...';

        try {
            const res = await fetch('/api/summarize', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ title: article.title, body: article.body, link: article.link, publisher: currentFeedName })
            });
            const data = await res.json();
            contentDiv.textContent = data.summary;
            // 로컬 articles 배열도 업데이트
            articles[idx].summary = data.summary;

            const daily = getDailyNews();
            daily.push({ title: article.title, link: article.link, summary: data.summary, source: currentFeedName });
            saveDailyNews(daily);

            btn.textContent = '선택됨';
            btn.classList.add('selected');

            const sumBtn = document.getElementById('article-' + idx).querySelector('.summarize-btn');
            if (sumBtn) { sumBtn.textContent = '요약 완료'; sumBtn.disabled = true; }
        } catch (e) {
            contentDiv.textContent = '요약 중 오류가 발생했습니다.';
            btn.textContent = 'Daily News 로 선택';
            btn.disabled = false;
        }
    }

    async function summarizeArticle(idx) {
        const articleEl = document.getElementById('article-' + idx);
        const btn = articleEl.querySelector('.summarize-btn');
        const summaryDiv = document.getElementById('summary-' + idx);
        const contentDiv = summaryDiv.querySelector('.summary-content');
        const article = articles[idx];

        btn.disabled = true;
        btn.textContent = '요약 중...';
        summaryDiv.classList.add('visible');
        contentDiv.textContent = 'AI가 요약하는 중입니다...';

        try {
            const res = await fetch('/api/summarize', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ title: article.title, body: article.body, link: article.link, publisher: currentFeedName })
            });
            const data = await res.json();
            contentDiv.textContent = data.summary;
            btn.textContent = '요약 완료';
            // 로컬 articles 배열도 업데이트
            articles[idx].summary = data.summary;
        } catch (e) {
            contentDiv.textContent = '요약 중 오류가 발생했습니다.';
            btn.textContent = '요약하기';
            btn.disabled = false;
        }
    }

    // Home 화면: DB 뉴스 통계 로드 (비동기, 실패해도 무방)
    function loadNewsStats() {
        var emptyDiv = document.getElementById('daily-empty');
        if (!emptyDiv) return;
        fetch('/api/news-stats')
            .then(function(res) { return res.json(); })
            .then(function(data) {
                if (data.total > 0) {
                    var statLines = '';
                    for (var pub in data.stats) {
                        statLines += '<li>' + escapeHtml(pub) + ': <strong>' + data.stats[pub] + '</strong>건</li>';
                    }
                    emptyDiv.innerHTML =
                        '<div class="home-icon">📰</div>' +
                        '<h2 class="home-title">DB에 총 ' + data.total + '건의 뉴스가 저장되어 있습니다</h2>' +
                        '<ul style="list-style:none;padding:0;margin:12px 0;font-size:15px;">' + statLines + '</ul>' +
                        '<p class="home-desc">왼쪽 메뉴에서 뉴스 제공자를 선택하여 기사를 확인하고<br>"Daily News 로 선택" 버튼을 눌러 기사를 추가하세요</p>';
                } else {
                    emptyDiv.innerHTML =
                        '<div class="home-icon">📰</div>' +
                        '<h2 class="home-title">Daily News가 비어 있습니다</h2>' +
                        '<p class="home-desc">"뉴스 업데이트" 버튼을 눌러 뉴스를 수집한 후<br>왼쪽 메뉴에서 뉴스 제공자를 선택하세요</p>';
                }
            })
            .catch(function() {});
    }

    // Home 화면: 선택된 Daily News 목록 렌더링
    function renderDailyList() {
        var listDiv = document.getElementById('daily-list');
        var emptyDiv = document.getElementById('daily-empty');
        var pptBtn = document.getElementById('daily-ppt-btn');
        var clearBtn = document.getElementById('daily-clear-btn');
        if (!listDiv) return;

        var daily = getDailyNews();
        if (daily.length === 0) {
            if (emptyDiv) emptyDiv.style.display = '';
            if (pptBtn) pptBtn.style.display = 'none';
            if (clearBtn) clearBtn.style.display = 'none';
            listDiv.innerHTML = '';
            loadNewsStats();
            return;
        }
        if (emptyDiv) emptyDiv.style.display = 'none';
        if (pptBtn) pptBtn.style.display = '';
        if (clearBtn) clearBtn.style.display = '';
        listDiv.innerHTML = daily.map(function(item, i) {
            return '<div class="daily-item">' +
                '<button class="daily-remove" onclick="removeDaily(' + i + ')">×</button>' +
                '<h3><span class="daily-order">' + (i + 1) + '</span><a href="' + escapeHtml(item.link) + '" target="_blank">' + escapeHtml(item.title) + '</a></h3>' +
                (item.source ? '<span class="daily-source">' + escapeHtml(item.source) + '</span>' : '') +
                '<div class="daily-summary">' + escapeHtml(item.summary) + '</div>' +
            '</div>';
        }).join('');
    }

    function removeDaily(idx) {
        const daily = getDailyNews();
        daily.splice(idx, 1);
        saveDailyNews(daily);
        renderDailyList();
    }

    function clearDaily() {
        if (confirm('선택한 Daily News를 모두 삭제하시겠습니까?')) {
            saveDailyNews([]);
            renderDailyList();
        }
    }

    async function downloadDailyPPT() {
        const daily = getDailyNews();
        if (daily.length === 0) { alert('선택된 뉴스가 없습니다.'); return; }
        try {
            const res = await fetch('/api/daily-ppt', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ articles: daily })
            });
            if (!res.ok) throw new Error('PPT 생성 실패');
            const blob = await res.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = 'daily_news.pptx';
            a.click();
            URL.revokeObjectURL(url);
        } catch (e) {
            alert('PPT 다운로드 중 오류가 발생했습니다.');
        }
    }

    // 뉴스 업데이트 버튼 클릭
    async function collectNews() {
        const btn = document.getElementById('collect-btn');
        btn.disabled = true;
        btn.textContent = '수집 중...';

        showSection('collect');
        document.getElementById('collect-status-text').textContent = '뉴스 정보를 수집 중입니다...';
        document.getElementById('collect-status-sub').textContent = '잠시만 기다려주세요';

        try {
            const res = await fetch('/api/collect-news', { method: 'POST' });
            const data = await res.json();
            newsCollected = true;
            document.getElementById('collect-status-text').textContent = '뉴스 정보 수집이 완료됐습니다';
            document.getElementById('collect-status-sub').textContent = '총 ' + data.collected + '건의 뉴스가 수집되었습니다. 왼쪽 메뉴에서 뉴스 제공자를 선택하세요.';
            if (data.errors && data.errors.length > 0) {
                document.getElementById('collect-status-sub').textContent += ' (일부 오류: ' + data.errors.join(', ') + ')';
            }
            setTimeout(() => {
                showSection('home');
                renderDailyList();
            }, 3000);
        } catch (e) {
            document.getElementById('collect-status-text').textContent = '뉴스 수집 중 오류가 발생했습니다';
            document.getElementById('collect-status-sub').textContent = '페이지를 새로고침해 주세요.';
        }
        btn.disabled = false;
        btn.textContent = '뉴스 업데이트';
    }

    // Home 탭 클릭
    document.querySelector('.feed-tab.home-tab').addEventListener('click', function(e) {
        e.preventDefault();
        setActiveTab(null);
        showSection('home');
        renderDailyList();
    });

    // 초기화: 홈 화면 표시
    showSection('home');
    renderDailyList();
    </script>
</body>
</html>
"""

def generate_ppt(articles):
    """뉴스 요약을 PPT로 생성 (슬라이드 1장당 2개 기사)"""
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)

    # 타이틀 슬라이드
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank
    txBox = slide.shapes.add_textbox(Inches(0.5), Inches(2.5), Inches(12.3), Inches(2))
    tf = txBox.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = "뉴스 핵심 요약"
    p.font.size = Pt(44)
    p.font.bold = True
    p.font.color.rgb = RGBColor(26, 115, 232)
    p.alignment = PP_ALIGN.CENTER

    p2 = tf.add_paragraph()
    p2.text = f"총 {len(articles)}건"
    p2.font.size = Pt(24)
    p2.font.color.rgb = RGBColor(100, 100, 100)
    p2.alignment = PP_ALIGN.CENTER

    # 기사 2개씩 슬라이드에 배치
    for i in range(0, len(articles), 2):
        slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank
        pair = articles[i:i+2]

        for j, article in enumerate(pair):
            left = Inches(0.5) if j == 0 else Inches(6.9)
            width = Inches(6.0)

            # 제목
            title_box = slide.shapes.add_textbox(left, Inches(0.4), width, Inches(1.0))
            tf_title = title_box.text_frame
            tf_title.word_wrap = True
            p_title = tf_title.paragraphs[0]
            p_title.text = article["title"]
            p_title.font.size = Pt(18)
            p_title.font.bold = True
            p_title.font.color.rgb = RGBColor(33, 33, 33)

            # 구분선
            line = slide.shapes.add_shape(
                1, left, Inches(1.5), width, Emu(0)
            )
            line.line.color.rgb = RGBColor(26, 115, 232)
            line.line.width = Pt(2)

            # 요약
            summary_box = slide.shapes.add_textbox(left, Inches(1.7), width, Inches(4.5))
            tf_summary = summary_box.text_frame
            tf_summary.word_wrap = True
            p_summary = tf_summary.paragraphs[0]
            p_summary.text = article.get("summary") or "요약 없음"
            p_summary.font.size = Pt(14)
            p_summary.font.color.rgb = RGBColor(44, 62, 80)
            p_summary.line_spacing = Pt(24)

            # URL
            url_box = slide.shapes.add_textbox(left, Inches(6.3), width, Inches(0.5))
            tf_url = url_box.text_frame
            tf_url.word_wrap = True
            p_url = tf_url.paragraphs[0]
            p_url.text = article.get("link", "")
            p_url.font.size = Pt(10)
            p_url.font.color.rgb = RGBColor(150, 150, 150)

    output = io.BytesIO()
    prs.save(output)
    output.seek(0)
    return output


@app.post("/api/collect-news")
async def api_collect_news():
    """활성화된 RSS 피드에서 뉴스를 수집하여 DB에 저장"""
    collected = 0
    errors = []
    for idx in ACTIVE_FEED_INDICES:
        feed = RSS_FEEDS[idx]
        try:
            articles = await parse_rss_and_fetch_news(feed["url"])
            if articles:
                save_articles_to_db(articles, publisher=feed["name"])
                collected += len(articles)
        except Exception as e:
            errors.append(f"{feed['name']}: {e}")
    return JSONResponse({
        "status": "completed",
        "collected": collected,
        "errors": errors,
    })


@app.get("/api/articles")
async def api_articles(publisher: str = ""):
    """DB에서 특정 제공자의 기사 목록 조회"""
    if not publisher:
        return JSONResponse({"articles": []})
    articles = load_articles_by_publisher(publisher)
    return JSONResponse({"articles": articles})


@app.post("/api/summarize")
async def api_summarize(request: Request):
    """개별 기사 요약 API"""
    data = await request.json()
    title = data.get("title", "")
    body = data.get("body", "")
    link = data.get("link", "")
    publisher = data.get("publisher", "")
    summary = await summarize_article(title, body)
    # DB의 summary 컬럼만 업데이트
    if summary and link:
        updated = update_article_summary(link, summary)
        if not updated:
            # fallback: 전체 upsert
            save_articles_to_db([{"title": title, "body": body, "link": link, "summary": summary}], publisher=publisher)
    return JSONResponse({"summary": summary})


@app.get("/api/news-stats")
async def api_news_stats():
    """DB에 저장된 뉴스 통계 조회"""
    stats = get_news_stats()
    total = sum(stats.values())
    return JSONResponse({"stats": stats, "total": total})


@app.post("/api/fetch-urls")
async def api_fetch_urls(request: Request):
    """URL 목록에서 뉴스 제목/본문 추출"""
    data = await request.json()
    urls = data.get("urls", [])
    fetch_tasks = [get_news_content(url) for url in urls[:20]]
    results = await asyncio.gather(*fetch_tasks)
    articles = []
    for (title, body, pub_date), url in zip(results, urls[:20]):
        is_error = title == "추출 실패" or body.startswith("[추출 실패]")
        is_paywall = body.startswith("[페이월/접근 제한]")
        articles.append({"title": title, "body": body, "link": url, "error": is_error, "paywall": is_paywall, "pub_date": pub_date})
    return JSONResponse({"articles": articles})


@app.post("/api/save-custom-articles")
async def api_save_custom_articles(request: Request):
    """직접 입력 기사를 DB에 저장"""
    data = await request.json()
    articles = data.get("articles", [])
    publisher = data.get("publisher", "직접 입력")
    save_articles_to_db(articles, publisher=publisher)
    return JSONResponse({"saved": len(articles)})


@app.post("/api/daily-ppt")
async def daily_ppt(request: Request):
    """Daily News 선택 기사들로 PPT 생성"""
    data = await request.json()
    articles = data.get("articles", [])
    if not articles:
        return JSONResponse({"error": "선택된 기사가 없습니다."}, status_code=400)
    output = generate_ppt(articles)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        headers={"Content-Disposition": "attachment; filename=daily_news.pptx"},
    )


@app.get("/", response_class=HTMLResponse)
async def get_index(feed: str = None):
    template = Template(HTML_TEMPLATE)
    active_feed = None

    if feed == "custom":
        active_feed = "custom"
    elif feed is not None:
        try:
            feed_idx = int(feed)
            if 0 <= feed_idx < len(RSS_FEEDS) and feed_idx in ACTIVE_FEED_INDICES:
                active_feed = feed_idx
        except ValueError:
            pass

    return HTMLResponse(content=template.render(
        feeds=RSS_FEEDS,
        active_feed=active_feed,
        active_indices=ACTIVE_FEED_INDICES,
        articles=[],
        error=None,
        db_error=supabase_error,
    ))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
