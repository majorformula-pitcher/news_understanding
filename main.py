import os
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse
import httpx
import asyncio
import re
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from jinja2 import Template
import anthropic
from supabase import create_client

app = FastAPI()

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

기사 제목: {title}
기사 본문: {body}"""

    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system="당신은 뉴스 요약 전문가입니다.",
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text.strip()
    except Exception as e:
        return f"요약 중 오류가 발생했습니다: {str(e)}"

async def get_news_content(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    try:
        async with httpx.AsyncClient(follow_redirects=True) as h_client:
            response = await h_client.get(url, headers=headers)
            response.raise_for_status()
            html_text = response.text
            
        soup = BeautifulSoup(html_text, 'html.parser')
        
        title_selectors = ['h2.title', 'h2.title_news', 'div.article_title h2', 'h1', 'h2']
        title_element = None
        for selector in title_selectors:
            title_element = soup.select_one(selector)
            if title_element and title_element.get_text(strip=True):
                break
        
        title = title_element.get_text(strip=True) if title_element else "제목을 찾을 수 없음"

        body_selectors = ['div.article_txt', 'div.article_body', 'div[itemprop="articleBody"]', 'div#articleBody', 'article', 'div.content']
        body_element = None
        for selector in body_selectors:
            body_element = soup.select_one(selector)
            if body_element and body_element.get_text(strip=True):
                break

        if body_element:
            body = body_element.get_text(separator='\n', strip=True)
            body = re.sub(r'(?:이메일|email|e-mail)\s*[:\s]*\s*[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', '', body, flags=re.IGNORECASE)
            body = re.sub(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', '', body)
            body = re.sub(r'[가-힣]{2,4}\s*기자(?!\w)', '', body)
            body = re.sub(r'\[\s*\]|\(\s*\)', '', body)
            body = '\n'.join([line.strip() for line in body.split('\n') if line.strip()])
        else:
            body = "본문을 찾을 수 없습니다."

        return title, body
    except Exception as e:
        return f"오류 발생: {e}", ""

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
        links = []
        for item in root.findall('.//item'):
            link_elem = item.find('link')
            if link_elem is not None and link_elem.text:
                links.append(link_elem.text)
                
        # 테스트를 위해 뉴스 1개만 처리하도록 수정 (links[:10])
        fetch_tasks = [get_news_content(link) for link in links[:10]]
        fetched_results = await asyncio.gather(*fetch_tasks)
        
        summary_tasks = [summarize_article(title, body) for title, body in fetched_results]
        summaries = await asyncio.gather(*summary_tasks)
        
        for (title, body), summary, link in zip(fetched_results, summaries, links[:10]):
            articles.append({
                'title': title,
                'body': body,
                'link': link,
                'summary': summary
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
        body { font-family: 'Pretendard', sans-serif; max-width: 1400px; margin: 0 auto; padding: 20px; background-color: #f0f2f5; }
        h1 { text-align: center; color: #1a73e8; margin-bottom: 30px; }
        .form-group { background: white; padding: 25px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); display: flex; gap: 10px; margin-bottom: 30px; }
        input[type="text"] { flex-grow: 1; padding: 15px; border: 2px solid #e0e0e0; border-radius: 8px; font-size: 16px; transition: border-color 0.3s; }
        input[type="text"]:focus { border-color: #1a73e8; outline: none; }
        button { padding: 15px 30px; background-color: #1a73e8; color: white; border: none; border-radius: 8px; cursor: pointer; font-size: 16px; font-weight: bold; }
        button:hover { background-color: #1557b0; }
        
        .result-item { background: white; margin-bottom: 40px; padding: 30px; border-radius: 15px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); }
        .result-item h2 { margin-top: 0; color: #333; font-size: 26px; border-bottom: 2px solid #f0f2f5; padding-bottom: 15px; }
        .result-item h2 a { text-decoration: none; color: inherit; }
        
        .article-layout { display: flex; gap: 30px; margin-top: 20px; align-items: flex-start; }
        .article-body { flex: 1.5; font-size: 16px; line-height: 1.8; color: #444; max-height: 600px; overflow-y: auto; padding-right: 15px; border-right: 1px solid #eee; }
        
        .summary-section { flex: 1.5; min-width: 420px; background-color: #fff9db; border: 2px solid #fab005; border-radius: 12px; padding: 25px; position: sticky; top: 20px; }
        .summary-section h3 { margin-top: 0; color: #e67e22; font-size: 20px; display: flex; align-items: center; gap: 8px; border-bottom: 1px solid #ffe066; padding-bottom: 10px; }
        .summary-section h3::before { content: '📋'; }
        .summary-content { font-size: 17px; color: #2c3e50; font-weight: 600; line-height: 1.6; white-space: pre-wrap; }
        
        .original-btn { display: inline-block; margin-top: 25px; padding: 10px 20px; border: 1px solid #1a73e8; color: #1a73e8; text-decoration: none; border-radius: 6px; font-size: 14px; transition: all 0.3s; }
        .original-btn:hover { background-color: #1a73e8; color: white; }
        
        .error-msg { background: #fff5f5; color: #c92a2a; padding: 15px; border-radius: 8px; border: 1px solid #ffc9c9; margin-bottom: 20px; }
        .loading-overlay { display: none; text-align: center; color: #1a73e8; font-weight: bold; font-size: 18px; margin-top: 20px; }
        
        @media (max-width: 900px) {
            .article-layout { flex-direction: column; }
            .article-body { border-right: none; border-bottom: 1px solid #eee; padding-bottom: 20px; padding-right: 0; }
            .summary-section { position: static; width: 100%; box-sizing: border-box; }
        }
    </style>
</head>
<body>
    <h1>🚀 뉴스 핵심 요약 서비스
        {% if db_error %}
        <div style="font-size: 14px; color: #c92a2a; background: #fff5f5; border: 1px solid #ffc9c9; border-radius: 8px; padding: 8px 15px; margin-top: 10px;">⚠️ Supabase 오류: {{ db_error }}</div>
        {% endif %}
    </h1>
    <form method="POST" onsubmit="document.getElementById('loading').style.display='block';">
        <div class="form-group">
            <input type="text" name="url" placeholder="뉴스 URL 또는 RSS 주소를 입력하세요" value="{{ url or 'https://rss.etnews.com/04046.xml' }}" required>
            <button type="submit">분석 및 요약</button>
        </div>
    </form>
    
    <div id="loading" class="loading-overlay">🤖 AI가 뉴스를 읽고 요약하는 중입니다...</div>

    {% if error %}
    <div class="error-msg">{{ error }}</div>
    {% endif %}

    {% if articles %}
        {% for article in articles %}
        <div class="result-item">
            <h2><a href="{{ article.link }}" target="_blank">{{ article.title }}</a></h2>
            <div class="article-layout">
                <div class="article-body">
                    {{ article.body }}
                </div>
                <div class="summary-section">
                    <h3>요약 뉴스</h3>
                    <div class="summary-content">{{ article.summary or '요약을 생성할 수 없습니다.' }}</div>
                </div>
            </div>
            <a href="{{ article.link }}" target="_blank" class="original-btn">원문 보기</a>
        </div>
        {% endfor %}
    {% endif %}
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
def get_index():
    template = Template(HTML_TEMPLATE)
    saved_articles = load_articles_from_db()
    return HTMLResponse(content=template.render(url="", articles=saved_articles, error=None, db_error=supabase_error))

@app.post("/", response_class=HTMLResponse)
async def post_index(request: Request):
    form_data = await request.form()
    url = form_data.get("url", "").strip()
    articles, error = [], None
    
    if url:
        try:
            if '.xml' in url or 'rss' in url.lower():
                articles = await parse_rss_and_fetch_news(url)
            else:
                title, body = await get_news_content(url)
                if title.startswith("오류 발생:"): error = title
                else:
                    summary = await summarize_article(title, body)
                    articles = [{'title': title, 'body': body, 'link': url, 'summary': summary}]
        except Exception as e:
            error = str(e)

    if articles:
        save_articles_to_db(articles)

    template = Template(HTML_TEMPLATE)
    return HTMLResponse(content=template.render(url=url, articles=articles, error=error, db_error=supabase_error))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
