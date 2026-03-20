from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse
import httpx
import asyncio
import re
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from jinja2 import Template

app = FastAPI()

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
                
        # To avoid extremely long processing times, we fetch them one by one.
        for link in links[:10]: # Limit to 10 articles to stay within Worker limits if necessary
            title, body = await get_news_content(link)
            articles.append({
                'title': title,
                'body': body,
                'link': link
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
        body { font-family: sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; }
        .form-group { margin-bottom: 20px; display: flex; gap: 10px; }
        input[type="text"] { flex-grow: 1; padding: 10px; box-sizing: border-box; font-size: 16px; }
        button { padding: 10px 20px; background-color: #007bff; color: white; border: none; cursor: pointer; font-size: 16px; white-space: nowrap; }
        .result { margin-top: 30px; border-top: 1px solid #ccc; padding-top: 20px; }
        .result h2 { margin-bottom: 10px; }
        .result h2 a { color: #007bff; text-decoration: none; }
        .result h2 a:hover { text-decoration: underline; }
        .body-text { white-space: pre-wrap; line-height: 1.6; font-size: 16px; color: #333; }
        .original-link { margin-top: 15px; display: inline-block; color: #666; font-size: 14px; text-decoration: none; border: 1px solid #ccc; padding: 5px 10px; border-radius: 4px; }
        .original-link:hover { background-color: #f8f9fa; color: #007bff; border-color: #007bff; }
        .error { color: red; margin-top: 20px; }
        .loading { display: none; margin-top: 20px; font-weight: bold; color: #007bff; }
    </style>
</head>
<body>
    <h1>뉴스 기사 추출기 (RSS 지원)</h1>
    <form method="POST" onsubmit="document.getElementById('loading').style.display='block';">
        <div class="form-group">
            <input type="text" name="url" placeholder="뉴스 URL 또는 RSS URL(예: https://rss.etnews.com/04046.xml)을 입력하세요" value="{{ url or 'https://rss.etnews.com/04046.xml' }}" required>
            <button type="submit">추출하기</button>
        </div>
    </form>
    
    <div id="loading" class="loading">기사를 불러오는 중입니다. 잠시만 기다려주세요...</div>

    {% if error %}
    <div class="error">{{ error }}</div>
    {% endif %}

    {% if articles %}
        <p>총 {{ articles|length }}개의 기사를 가져왔습니다.</p>
        {% for article in articles %}
        <div class="result">
            <h2><a href="{{ article.link }}" target="_blank" rel="noopener noreferrer">{{ article.title }}</a></h2>
            <div class="body-text">{{ article.body }}</div>
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
                articles = [{'title': title, 'body': body, 'link': url}]
            
    template = Template(HTML_TEMPLATE)
    html_content = template.render(url=url, articles=articles, error=error)
    return HTMLResponse(content=html_content)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)