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
    {"name": "Hugging Face", "url": "https://huggingface.co/blog/feed.xml"},
]

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


def save_articles_to_db(articles):
    """뉴스 기사를 Supabase에 저장. 오류 발생 시 메시지 반환"""
    global supabase_error
    supabase_error = None
    if not supabase:
        return
    for article in articles:
        try:
            supabase.table("news-understanding").upsert(
                {
                    "title": article["title"],
                    "content": article["body"],
                    "summary": article["summary"],
                    "url": article["link"],
                },
                on_conflict="url",
            ).execute()
        except Exception as e:
            supabase_error = f"DB 저장 오류: {e}"
            print(supabase_error)


def load_articles_from_db():
    """Supabase에서 저장된 기사 목록 조회"""
    global supabase_error
    supabase_error = None
    if not supabase:
        return []
    try:
        result = (
            supabase.table("news-understanding")
            .select("*")
            .order("created_at", desc=True)
            .limit(50)
            .execute()
        )
        return [
            {
                "title": row["title"],
                "body": row["content"],
                "summary": row["summary"],
                "link": row["url"],
            }
            for row in result.data
        ]
    except Exception as e:
        supabase_error = f"DB 조회 오류: {e}"
        print(supabase_error)
        return []

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
                    return err_title, reason + "\n\n" + err_desc
                if status_code == 403:
                    return "추출 실패", "[추출 실패] HTTP 403 Forbidden — 이 사이트는 봇 접근을 차단하고 있습니다. (페이월 또는 봇 방지)"
                elif status_code == 401:
                    return "추출 실패", "[추출 실패] HTTP 401 Unauthorized — 로그인이 필요한 페이지입니다."
                else:
                    return "추출 실패", f"[추출 실패] HTTP {status_code} — 서버에서 요청을 거부했습니다."

        # JavaScript 렌더링 전용 페이지 감지
        if len(html_text.strip()) < 500 and ('javascript' in html_text.lower() or 'noscript' in html_text.lower()):
            error_reason = "[추출 실패] 이 페이지는 JavaScript로 렌더링되어 서버에서 직접 추출할 수 없습니다."
            return "추출 실패", error_reason

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
                return title or "추출 실패", error_reason

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
                return title or "추출 실패", "[페이월/접근 제한] 전체 본문을 가져올 수 없어 요약 정보만 표시합니다.\n\n" + fallback
            error_reason = f"[추출 실패] 본문이 너무 짧습니다 ({len(body)}자). 페이월이 있거나 JavaScript로 렌더링되는 페이지일 수 있습니다."
            return title or "추출 실패", error_reason

        return title, body
    except httpx.TimeoutException:
        return "추출 실패", "[추출 실패] 요청 시간이 초과되었습니다 (15초). 서버가 응답하지 않거나 봇 접근을 차단하고 있을 수 있습니다."
    except httpx.ConnectError:
        return "추출 실패", f"[추출 실패] 서버에 연결할 수 없습니다. URL을 확인해주세요: {url}"
    except Exception as e:
        return "추출 실패", f"[추출 실패] {type(e).__name__}: {e}"

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
                items.append({'link': link_elem.text, 'rss_title': rss_title, 'rss_desc': rss_desc})

        fetch_tasks = [get_news_content(it['link']) for it in items[:10]]
        fetched_results = await asyncio.gather(*fetch_tasks)

        for (title, body), it in zip(fetched_results, items[:10]):
            # 페이지 접근 실패 시 RSS 데이터로 대체
            if title.startswith("오류 발생:") or not body or body == "본문을 찾을 수 없습니다.":
                title = it['rss_title'] or title
                body = it['rss_desc'] or body
            articles.append({
                'title': title,
                'body': body,
                'link': it['link'],
                'summary': ''
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
        <a href="/"
           class="feed-tab home-tab {% if active_feed is none %}active{% endif %}">
            Home
        </a>
        {% for feed in feeds %}
        <a href="/?feed={{ loop.index0 }}"
           class="feed-tab {% if active_feed == loop.index0 %}active{% endif %}"
           onclick="document.getElementById('loading').style.display='block';">
            {{ feed.name }}
        </a>
        {% endfor %}
        <a href="/?feed=custom"
           class="feed-tab {% if active_feed == 'custom' %}active{% endif %}">
            직접 입력
        </a>
    </nav>

    <!-- 오른쪽 콘텐츠 영역 -->
    <main class="content">
        {% if db_error %}
        <div class="db-error">Supabase 오류: {{ db_error }}</div>
        {% endif %}

        {% if active_feed is not none and active_feed != 'custom' %}
        <div class="content-header">
            <h1>{{ feeds[active_feed].name }}</h1>
        </div>
        {% endif %}

        <div id="loading" class="loading-overlay">뉴스를 불러오는 중입니다...</div>

        {% if error %}
        <div class="error-msg">{{ error }}</div>
        {% endif %}

        {% if articles %}
            {% for article in articles %}
            <div class="result-item" id="article-{{ loop.index0 }}">
                <h2 style="display:flex;align-items:center;justify-content:space-between;gap:12px;">
                    <a href="{{ article.link }}" target="_blank" style="flex:1;">{{ article.title }}</a>
                    <button class="daily-btn" onclick="selectForDaily({{ loop.index0 }})" id="daily-btn-{{ loop.index0 }}">Daily News 로 선택</button>
                </h2>
                <div class="article-layout">
                    <div class="article-body">{{ article.body }}</div>
                    <div class="summary-section" id="summary-{{ loop.index0 }}">
                        <h3>요약</h3>
                        <div class="summary-content"></div>
                    </div>
                </div>
                <div class="btn-row">
                    <a href="{{ article.link }}" target="_blank" class="original-btn">원문 보기</a>
                    <button class="summarize-btn" onclick="summarizeArticle({{ loop.index0 }})">요약하기</button>
                </div>
            </div>
            {% endfor %}
        {% elif active_feed == 'custom' %}
            <div class="content-header">
                <h1>직접 입력</h1>
            </div>
            <div class="result-item">
                <textarea id="custom-input" placeholder="뉴스 URL이 포함된 텍스트를 붙여넣으세요..." style="width:100%;height:180px;padding:15px;border:1px solid #ddd;border-radius:8px;font-size:15px;line-height:1.6;resize:vertical;font-family:inherit;"></textarea>
                <div style="margin-top:12px;">
                    <button onclick="extractUrls()" class="summarize-btn" id="extract-btn" style="padding:10px 24px;font-size:15px;">뉴스 추출</button>
                </div>
            </div>
            <div id="custom-loading" class="loading-overlay">URL에서 뉴스를 불러오는 중입니다...</div>
            <div id="custom-articles"></div>
        {% elif active_feed is none %}
            <div class="content-header">
                <h1>Daily News</h1>
                <div style="display:flex;gap:10px;">
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
        {% endif %}
    </main>

    <script>
    const articles = [
        {% for article in articles %}
        { title: {{ article.title | tojson }}, body: {{ article.body | tojson }}, link: {{ article.link | tojson }} },
        {% endfor %}
    ];
    const currentFeedName = {{ (feeds[active_feed].name if active_feed is not none and active_feed != 'custom' else "") | tojson }};

    // Daily News localStorage 관리
    function getDailyNews() {
        try { return JSON.parse(localStorage.getItem('dailyNews') || '[]'); }
        catch { return []; }
    }
    function saveDailyNews(list) {
        localStorage.setItem('dailyNews', JSON.stringify(list));
    }

    // 페이지 로드 시 이미 선택된 기사 버튼 상태 업데이트
    function updateDailyBtnStates() {
        const daily = getDailyNews();
        const dailyLinks = daily.map(d => d.link);
        articles.forEach((a, idx) => {
            const btn = document.getElementById('daily-btn-' + idx);
            if (btn && dailyLinks.includes(a.link)) {
                btn.textContent = '선택됨';
                btn.disabled = true;
                btn.classList.add('selected');
            }
        });
    }

    // Daily News 로 선택 버튼 클릭
    async function selectForDaily(idx) {
        const btn = document.getElementById('daily-btn-' + idx);
        const article = articles[idx];

        btn.disabled = true;
        btn.textContent = '요약 중...';

        // 요약도 같이 표시
        const summaryDiv = document.getElementById('summary-' + idx);
        const contentDiv = summaryDiv.querySelector('.summary-content');
        summaryDiv.classList.add('visible');
        contentDiv.textContent = 'AI가 요약하는 중입니다...';

        try {
            const res = await fetch('/api/summarize', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(article)
            });
            const data = await res.json();
            contentDiv.textContent = data.summary;

            // localStorage에 저장
            const daily = getDailyNews();
            daily.push({ title: article.title, link: article.link, summary: data.summary, source: currentFeedName });
            saveDailyNews(daily);

            btn.textContent = '선택됨';
            btn.classList.add('selected');

            // 요약하기 버튼도 완료 처리
            const sumBtn = document.querySelectorAll('.summarize-btn')[idx];
            if (sumBtn) { sumBtn.textContent = '요약 완료'; sumBtn.disabled = true; }
        } catch (e) {
            contentDiv.textContent = '요약 중 오류가 발생했습니다.';
            btn.textContent = 'Daily News 로 선택';
            btn.disabled = false;
        }
    }

    async function summarizeArticle(idx) {
        const btn = document.querySelectorAll('.summarize-btn')[idx];
        const summaryDiv = document.getElementById('summary-' + idx);
        const contentDiv = summaryDiv.querySelector('.summary-content');

        btn.disabled = true;
        btn.textContent = '요약 중...';
        summaryDiv.classList.add('visible');
        contentDiv.textContent = 'AI가 요약하는 중입니다...';

        try {
            const res = await fetch('/api/summarize', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(articles[idx])
            });
            const data = await res.json();
            contentDiv.textContent = data.summary;
            btn.textContent = '요약 완료';
        } catch (e) {
            contentDiv.textContent = '요약 중 오류가 발생했습니다.';
            btn.textContent = '요약하기';
            btn.disabled = false;
        }
    }

    // Home 화면: 선택된 Daily News 목록 렌더링
    function renderDailyList() {
        const listDiv = document.getElementById('daily-list');
        const emptyDiv = document.getElementById('daily-empty');
        const pptBtn = document.getElementById('daily-ppt-btn');
        const clearBtn = document.getElementById('daily-clear-btn');
        if (!listDiv) return;

        const daily = getDailyNews();
        if (daily.length === 0) {
            if (emptyDiv) emptyDiv.style.display = '';
            if (pptBtn) pptBtn.style.display = 'none';
            if (clearBtn) clearBtn.style.display = 'none';
            listDiv.innerHTML = '';
            return;
        }
        if (emptyDiv) emptyDiv.style.display = 'none';
        if (pptBtn) pptBtn.style.display = '';
        if (clearBtn) clearBtn.style.display = '';
        listDiv.innerHTML = daily.map((item, i) =>
            '<div class="daily-item">' +
                '<button class="daily-remove" onclick="removeDaily(' + i + ')">×</button>' +
                '<h3><span class="daily-order">' + (i + 1) + '</span><a href="' + item.link + '" target="_blank">' + item.title + '</a></h3>' +
                (item.source ? '<span class="daily-source">' + item.source + '</span>' : '') +
                '<div class="daily-summary">' + item.summary + '</div>' +
            '</div>'
        ).join('');
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

    // 직접 입력: URL 추출 및 기사 표시
    let customArticles = [];

    async function extractUrls() {
        const input = document.getElementById('custom-input');
        const btn = document.getElementById('extract-btn');
        const loadingDiv = document.getElementById('custom-loading');
        const container = document.getElementById('custom-articles');
        if (!input || !input.value.trim()) return;

        // URL 추출
        const urlRegex = /https?:\/\/[^\s<>"')\]]+/g;
        const urls = [...new Set(input.value.match(urlRegex) || [])];
        if (urls.length === 0) { alert('URL을 찾을 수 없습니다.'); return; }

        btn.disabled = true;
        btn.textContent = '추출 중...';
        if (loadingDiv) loadingDiv.style.display = 'block';
        container.innerHTML = '';
        customArticles = [];

        try {
            const res = await fetch('/api/fetch-urls', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ urls: urls })
            });
            const data = await res.json();
            customArticles = data.articles || [];

            container.innerHTML = customArticles.map((a, i) => {
                const cssClass = a.error ? ' extraction-error' : (a.paywall ? ' extraction-partial' : '');
                const displayTitle = a.error ? '⚠ ' + (a.title !== '추출 실패' ? a.title : new URL(a.link).hostname) : a.title;
                const bodyHtml = a.error ? '<div class="error-detail">' + a.body + '</div>'
                    : a.paywall ? '<div class="paywall-notice">⚠ 페이월 또는 접근 제한으로 전체 본문을 가져올 수 없습니다. 요약 정보만 표시됩니다.</div>' + a.body.replace(/^\[페이월\\/접근 제한\][^\\n]*\\n\\n/, '')
                    : a.body;
                const showActions = !a.error;
                return '<div class="result-item' + cssClass + '" id="custom-article-' + i + '">' +
                    '<h2 style="display:flex;align-items:center;justify-content:space-between;gap:12px;">' +
                        '<a href="' + a.link + '" target="_blank" style="flex:1;">' + displayTitle + '</a>' +
                        (showActions ? '<button class="daily-btn" onclick="selectCustomForDaily(' + i + ')" id="custom-daily-btn-' + i + '">Daily News 로 선택</button>' : '') +
                    '</h2>' +
                    '<div class="article-layout">' +
                        '<div class="article-body">' + bodyHtml + '</div>' +
                        (showActions ?
                        '<div class="summary-section" id="custom-summary-' + i + '">' +
                            '<h3>요약</h3>' +
                            '<div class="summary-content"></div>' +
                        '</div>' : '') +
                    '</div>' +
                    '<div class="btn-row">' +
                        '<a href="' + a.link + '" target="_blank" class="original-btn">원문 보기</a>' +
                        (showActions ? '<button class="summarize-btn" id="custom-sum-btn-' + i + '" onclick="summarizeCustom(' + i + ')">요약하기</button>' : '') +
                    '</div>' +
                '</div>';
            }).join('');

            // 이미 선택된 기사 버튼 상태 업데이트
            const daily = getDailyNews();
            const dailyLinks = daily.map(d => d.link);
            customArticles.forEach((a, i) => {
                const dbtn = document.getElementById('custom-daily-btn-' + i);
                if (dbtn && dailyLinks.includes(a.link)) {
                    dbtn.textContent = '선택됨';
                    dbtn.disabled = true;
                    dbtn.classList.add('selected');
                }
            });
        } catch (e) {
            container.innerHTML = '<div class="error-msg">URL 처리 중 오류가 발생했습니다.</div>';
        }
        btn.disabled = false;
        btn.textContent = '뉴스 추출';
        if (loadingDiv) loadingDiv.style.display = 'none';
    }

    async function summarizeCustom(idx) {
        const btn = document.getElementById('custom-sum-btn-' + idx);
        const summaryDiv = document.getElementById('custom-summary-' + idx);
        const contentDiv = summaryDiv.querySelector('.summary-content');

        btn.disabled = true;
        btn.textContent = '요약 중...';
        summaryDiv.classList.add('visible');
        contentDiv.textContent = 'AI가 요약하는 중입니다...';

        try {
            const res = await fetch('/api/summarize', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(customArticles[idx])
            });
            const data = await res.json();
            contentDiv.textContent = data.summary;
            btn.textContent = '요약 완료';
        } catch (e) {
            contentDiv.textContent = '요약 중 오류가 발생했습니다.';
            btn.textContent = '요약하기';
            btn.disabled = false;
        }
    }

    async function selectCustomForDaily(idx) {
        const btn = document.getElementById('custom-daily-btn-' + idx);
        const article = customArticles[idx];

        btn.disabled = true;
        btn.textContent = '요약 중...';

        const summaryDiv = document.getElementById('custom-summary-' + idx);
        const contentDiv = summaryDiv.querySelector('.summary-content');
        summaryDiv.classList.add('visible');
        contentDiv.textContent = 'AI가 요약하는 중입니다...';

        try {
            const res = await fetch('/api/summarize', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(article)
            });
            const data = await res.json();
            contentDiv.textContent = data.summary;

            const daily = getDailyNews();
            daily.push({ title: article.title, link: article.link, summary: data.summary, source: '직접 입력' });
            saveDailyNews(daily);

            btn.textContent = '선택됨';
            btn.classList.add('selected');

            const sumBtn = document.getElementById('custom-sum-btn-' + idx);
            if (sumBtn) { sumBtn.textContent = '요약 완료'; sumBtn.disabled = true; }
        } catch (e) {
            contentDiv.textContent = '요약 중 오류가 발생했습니다.';
            btn.textContent = 'Daily News 로 선택';
            btn.disabled = false;
        }
    }

    // 초기화
    updateDailyBtnStates();
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


@app.post("/api/summarize")
async def api_summarize(request: Request):
    """개별 기사 요약 API"""
    data = await request.json()
    title = data.get("title", "")
    body = data.get("body", "")
    link = data.get("link", "")
    summary = await summarize_article(title, body)
    # DB에도 저장
    if summary and link:
        save_articles_to_db([{"title": title, "body": body, "link": link, "summary": summary}])
    return JSONResponse({"summary": summary})


@app.post("/api/fetch-urls")
async def api_fetch_urls(request: Request):
    """URL 목록에서 뉴스 제목/본문 추출"""
    data = await request.json()
    urls = data.get("urls", [])
    fetch_tasks = [get_news_content(url) for url in urls[:20]]
    results = await asyncio.gather(*fetch_tasks)
    articles = []
    for (title, body), url in zip(results, urls[:20]):
        is_error = title == "추출 실패" or body.startswith("[추출 실패]")
        is_paywall = body.startswith("[페이월/접근 제한]")
        articles.append({"title": title, "body": body, "link": url, "error": is_error, "paywall": is_paywall})
    return JSONResponse({"articles": articles})


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
    articles, error = [], None
    active_feed = None

    if feed == "custom":
        active_feed = "custom"
    elif feed is not None:
        try:
            feed_idx = int(feed)
            if 0 <= feed_idx < len(RSS_FEEDS):
                active_feed = feed_idx
                rss_url = RSS_FEEDS[feed_idx]["url"]
                articles = await parse_rss_and_fetch_news(rss_url)
                if articles:
                    save_articles_to_db(articles)
        except (ValueError, Exception) as e:
            error = str(e)

    return HTMLResponse(content=template.render(
        feeds=RSS_FEEDS,
        active_feed=active_feed,
        articles=articles,
        error=error,
        db_error=supabase_error,
    ))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
