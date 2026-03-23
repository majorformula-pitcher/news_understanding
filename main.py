import os
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse
import httpx
import asyncio
import re
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from jinja2 import Template
import google.generativeai as genai

app = FastAPI()

# Configure Gemini using environment variable
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)
else:
    print("Warning: GOOGLE_API_KEY environment variable is not set.")

async def summarize_article(title, body):
    if not GOOGLE_API_KEY:
        return "API 키가 설정되지 않아 요약을 생성할 수 없습니다."
    
    if not body or body == "Content not found" or len(body) < 100:
        return None
    
    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        prompt = f"""
        당신은 뉴스 요약 전문가입니다. 다음 뉴스 기사를 읽고 아래 형식에 맞춰 요약해 주세요.
        기사 제목을 먼저 쓰고, 그 아래에 핵심 내용을 딱 2줄로 요약해야 합니다.
        각 요약 줄은 '.'으로 시작해야 합니다.
        
        형식 예시:
        [기사 제목]
        . 요약 내용 첫 번째 줄
        . 요약 내용 두 번째 줄
        
        기사 제목: {title}
        기사 본문: {body}
        """
        
        response = await asyncio.to_thread(model.generate_content, prompt)
        return response.text.strip()
    except Exception as e:
        print(f"Error summarizing: {e}")
        return None

async def get_news_content(url):
    # Set User-Agent to avoid being blocked
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            html_text = response.text
            
        soup = BeautifulSoup(html_text, 'html.parser')
        
        # Try multiple common selectors for ETNews title
        title_selectors = ['h2.title', 'h2.title_news', 'div.article_title h2', 'h2']
        title_element = None
        for selector in title_selectors:
            title_element = soup.select_one(selector)
            if title_element and title_element.get_text(strip=True):
                break
        
        title = title_element.get_text(strip=True) if title_element else "Title not found"

        # Try multiple common selectors for ETNews body
        body_selectors = ['div.article_txt', 'div.article_body', 'div[itemprop="articleBody"]', 'div#articleBody', 'article']
        body_element = None
        for selector in body_selectors:
            body_element = soup.select_one(selector)
            if body_element and body_element.get_text(strip=True):
                break

        if body_element:
            # Extract text, converting block elements and <br> tags to newlines
            body = body_element.get_text(separator='\n', strip=True)
            
            # Remove email addresses and common labels (case-insensitive)
            email_pattern = r'(?:이메일|email|e-mail)\s*[:\s]*\s*[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
            body = re.sub(email_pattern, '', body, flags=re.IGNORECASE)
            body = re.sub(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', '', body)
            
            # Remove reporter names (e.g., "홍길동 기자", "김철수 기자")
            body = re.sub(r'[가-힣]{2,4}\s*기자(?!\w)', '', body)
            
            # Clean up remaining empty brackets or parentheses
            body = re.sub(r'\[\s*\]|\(\s*\)', '', body)

            # Clean up multiple consecutive newlines and leading/trailing whitespace
            body = '\n'.join([line.strip() for line in body.split('\n') if line.strip()])
        else:
            body = "Content not found"

        return title, body
            
    except Exception as e:
        return f"An error occurred: {e}", ""

async def parse_rss_and_fetch_news(rss_url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    articles = []
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(rss_url, headers=headers)
            response.raise_for_status()
            content = response.text
            
        root = ET.fromstring(content)
        # Find all <item> elements and extract their <link>
        links = []
        for item in root.findall('.//item'):
            link_elem = item.find('link')
            if link_elem is not None and link_elem.text:
                links.append(link_elem.text)
                
        # To avoid extremely long processing times, we fetch them in parallel.
        fetch_tasks = [get_news_content(link) for link in links[:10]]
        fetched_results = await asyncio.gather(*fetch_tasks)
        
        # Then summarize them in parallel
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
    <title>뉴스 기사 추출기</title>
    <style>
        body { font-family: sans-serif; max-width: 1000px; margin: 0 auto; padding: 20px; background-color: #f8f9fa; }
        h1 { text-align: center; color: #333; }
        .form-group { margin-bottom: 20px; display: flex; gap: 10px; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }
        input[type="text"] { flex-grow: 1; padding: 12px; border: 1px solid #ddd; border-radius: 4px; font-size: 16px; }
        button { padding: 12px 24px; background-color: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 16px; white-space: nowrap; font-weight: bold; }
        button:hover { background-color: #0056b3; }
        .result { margin-top: 30px; background: white; padding: 25px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); }
        .result h2 { margin-top: 0; margin-bottom: 15px; font-size: 24px; }
        .result h2 a { color: #333; text-decoration: none; }
        .result h2 a:hover { color: #007bff; }
        
        .article-container { display: flex; gap: 25px; margin-top: 20px; flex-wrap: wrap; }
        .body-text { flex: 3; min-width: 300px; white-space: pre-wrap; line-height: 1.8; font-size: 16px; color: #444; }
        .summary-box { flex: 1.2; min-width: 250px; background-color: #ebf5ff; border: 1px solid #b3d7ff; padding: 20px; border-radius: 10px; height: fit-content; position: sticky; top: 20px; }
        .summary-box h3 { margin-top: 0; font-size: 18px; color: #0056b3; border-bottom: 2px solid #b3d7ff; padding-bottom: 10px; margin-bottom: 15px; }
        .summary-content { font-size: 15px; line-height: 1.6; color: #2c3e50; font-weight: 500; white-space: pre-wrap; }
        
        .original-link { margin-top: 20px; display: inline-block; color: #666; font-size: 14px; text-decoration: none; border: 1px solid #ccc; padding: 8px 16px; border-radius: 4px; }
        .original-link:hover { background-color: #f8f9fa; color: #007bff; border-color: #007bff; }
        .error { color: #dc3545; background: #f8d7da; padding: 15px; border-radius: 4px; margin-top: 20px; }
        .loading { display: none; text-align: center; margin-top: 20px; font-weight: bold; color: #007bff; }
    </style>
</head>
<body>
    <h1>뉴스 기사 추출 및 요약 (Gemini AI)</h1>
    <form method="POST" onsubmit="document.getElementById('loading').style.display='block';">
        <div class="form-group">
            <input type="text" name="url" placeholder="뉴스 URL 또는 RSS URL을 입력하세요" value="{{ url or 'https://rss.etnews.com/04046.xml' }}" required>
            <button type="submit">추출 및 요약하기</button>
        </div>
    </form>
    
    <div id="loading" class="loading">AI가 기사를 분석하고 요약 중입니다. 잠시만 기다려주세요...</div>

    {% if error %}
    <div class="error">{{ error }}</div>
    {% endif %}

    {% if articles %}
        <p style="text-align: right; color: #666;">총 {{ articles|length }}개의 기사를 가져왔습니다.</p>
        {% for article in articles %}
        <div class="result">
            <h2><a href="{{ article.link }}" target="_blank" rel="noopener noreferrer">{{ article.title }}</a></h2>
            
            <div class="article-container">
                <div class="body-text">{{ article.body }}</div>
                
                {% if article.summary %}
                <div class="summary-box">
                    <h3>Gemini AI 요약</h3>
                    <div class="summary-content">{{ article.summary }}</div>
                </div>
                {% endif %}
            </div>
            
            <a href="{{ article.link }}" target="_blank" rel="noopener noreferrer" class="original-link">원문 기사 보기 →</a>
        </div>
        {% endfor %}
    {% endif %}
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
def get_index():
    template = Template(HTML_TEMPLATE)
    html_content = template.render(url="", articles=[], error=None)
    return HTMLResponse(content=html_content)

@app.post("/", response_class=HTMLResponse)
async def post_index(request: Request):
    url = ""
    articles = []
    error = None
    
    form_data = await request.form()
    url = form_data.get("url")
    
    if url:
        url = url.strip()
        if '.xml' in url or 'rss' in url.lower():
            try:
                articles = await parse_rss_and_fetch_news(url)
            except Exception as e:
                error = str(e)
        else:
            title, body = await get_news_content(url)
            if title.startswith("An error occurred:"):
                error = title
            else:
                summary = await summarize_article(title, body)
                articles = [{'title': title, 'body': body, 'link': url, 'summary': summary}]
            
    template = Template(HTML_TEMPLATE)
    html_content = template.render(url=url, articles=articles, error=error)
    return HTMLResponse(content=html_content)

if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)