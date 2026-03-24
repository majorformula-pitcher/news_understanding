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
    {"name": "ZDNet", "url": "https://zdnet.co.kr/rss/all.xml"},
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

        body_selectors = ['div.article_txt', 'div.article_body', '[itemprop="articleBody"]', 'div#articleBody', 'div.entry-content', 'div.blog-content', 'article', 'div.content']
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
        .article-body { flex: 0 0 auto; width: 100%; font-size: 15px; line-height: 1.8; color: #444; max-height: 500px; overflow-y: auto; padding-right: 15px; }

        .summary-section { flex: 0 0 400px; background-color: #fff9db; border: 2px solid #fab005; border-radius: 12px; padding: 20px; position: sticky; top: 20px; display: none; }
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
        .loading-overlay { display: none; text-align: center; color: #1a73e8; font-weight: bold; font-size: 18px; padding: 40px 0; }
        .db-error { font-size: 13px; color: #c92a2a; background: #fff5f5; border: 1px solid #ffc9c9; border-radius: 8px; padding: 8px 15px; margin-bottom: 15px; }
        .empty-state { text-align: center; color: #888; padding: 60px 20px; font-size: 18px; }

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
        {% for feed in feeds %}
        <a href="/?feed={{ loop.index0 }}"
           class="feed-tab {% if active_feed == loop.index0 %}active{% endif %}"
           onclick="document.getElementById('loading').style.display='block';">
            {{ feed.name }}
        </a>
        {% endfor %}
    </nav>

    <!-- 오른쪽 콘텐츠 영역 -->
    <main class="content">
        {% if db_error %}
        <div class="db-error">Supabase 오류: {{ db_error }}</div>
        {% endif %}

        <div class="content-header">
            <h1>{% if active_feed is not none %}{{ feeds[active_feed].name }}{% else %}뉴스 핵심 요약 서비스{% endif %}</h1>
            {% if articles %}
            <a href="/download-ppt" class="ppt-btn">PPT 다운로드 ({{ (articles|length + 1) // 2 + 1 }}장)</a>
            {% endif %}
        </div>

        <div id="loading" class="loading-overlay">뉴스를 불러오는 중입니다...</div>

        {% if error %}
        <div class="error-msg">{{ error }}</div>
        {% endif %}

        {% if articles %}
            {% for article in articles %}
            <div class="result-item" id="article-{{ loop.index0 }}">
                <h2><a href="{{ article.link }}" target="_blank">{{ article.title }}</a></h2>
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
        {% elif active_feed is none %}
            <div class="empty-state">왼쪽에서 뉴스 제공자를 선택하세요.</div>
        {% endif %}
    </main>

    <script>
    const articles = [
        {% for article in articles %}
        { title: {{ article.title | tojson }}, body: {{ article.body | tojson }}, link: {{ article.link | tojson }} },
        {% endfor %}
    ];

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


@app.get("/download-ppt")
def download_ppt():
    articles = load_articles_from_db()
    if not articles:
        return HTMLResponse(content="<h3>다운로드할 기사가 없습니다.</h3>", status_code=404)
    output = generate_ppt(articles)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        headers={"Content-Disposition": "attachment; filename=news_summary.pptx"},
    )


@app.get("/", response_class=HTMLResponse)
async def get_index(feed: int = None):
    template = Template(HTML_TEMPLATE)
    articles, error = [], None
    active_feed = feed

    if feed is not None and 0 <= feed < len(RSS_FEEDS):
        try:
            rss_url = RSS_FEEDS[feed]["url"]
            articles = await parse_rss_and_fetch_news(rss_url)
            if articles:
                save_articles_to_db(articles)
        except Exception as e:
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
