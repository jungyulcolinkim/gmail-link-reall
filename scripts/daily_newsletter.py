#!/usr/bin/env python3
"""정열님의 매일 아침 뉴스레터 — RSS 기반 큐레이션 + Claude 요약."""
import json
import os
import re
import smtplib
import ssl
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from urllib.parse import quote

import anthropic
import feedparser
import requests

# 한국 시간(KST = UTC+9)
KST = timezone(timedelta(hours=9))
WEEKDAYS = ['월', '화', '수', '목', '금', '토', '일']
DAYS_WINDOW = 3  # 최근 N일 이내 뉴스만


def kst_today_kor() -> str:
    now = datetime.now(KST)
    return f"{now.year}년 {now.month:02d}월 {now.day:02d}일 ({WEEKDAYS[now.weekday()]})"


def today_iso() -> str:
    return datetime.now(KST).strftime('%Y-%m-%d')


# ----------------------------------------------------------------------------
# 1) RSS 카테고리별 검색 쿼리 정의
# ----------------------------------------------------------------------------

CATEGORY_DEFS = [
    {
        'key': 'domestic',
        'title': '🌐 국내외 뉴스',
        'color': '#1a73e8',
        'queries': [
            ('한국 정치', 'ko'),
            ('한국 사회', 'ko'),
            ('전쟁 외교', 'ko'),
            ('우크라이나 가자 중동', 'ko'),
        ],
    },
    {
        'key': 'tech',
        'title': '💻 IT / 테크',
        'color': '#10b981',
        'queries': [
            ('빅테크 반도체', 'ko'),
            ('IT 신기술', 'ko'),
            ('big tech news', 'en'),
            ('semiconductor chip', 'en'),
        ],
    },
    {
        'key': 'ai',
        'title': '🤖 AI',
        'color': '#8b5cf6',
        'queries': [
            ('AI 인공지능', 'ko'),
            ('Claude Anthropic', 'en'),
            ('ChatGPT OpenAI', 'en'),
            ('Gemini Google AI', 'en'),
        ],
    },
    {
        'key': 'finance',
        'title': '💰 금융 / 경제',
        'color': '#f59e0b',
        'queries': [
            ('환율 코스피', 'ko'),
            ('국제유가 WTI', 'ko'),
            ('Fed 금리 미국 증시', 'ko'),
            ('oil price stock market', 'en'),
        ],
    },
]


def google_news_rss_url(query: str, lang: str = 'ko') -> str:
    """Google News RSS URL을 만든다 (lang='ko' 또는 'en')."""
    if lang == 'en':
        return f'https://news.google.com/rss/search?q={quote(query)}&hl=en-US&gl=US&ceid=US:en'
    return f'https://news.google.com/rss/search?q={quote(query)}&hl=ko&gl=KR&ceid=KR:ko'


def fetch_rss_items(url: str, max_items: int = 20) -> list[dict]:
    """RSS 피드를 받아 표준화된 item 목록을 반환."""
    try:
        feed = feedparser.parse(url)
    except Exception as e:
        print(f'  ⚠️ RSS fetch 실패: {e}', flush=True)
        return []

    items = []
    for entry in feed.entries[:max_items]:
        # 발행일 파싱 (RSS pubDate)
        pub_dt = None
        if getattr(entry, 'published_parsed', None):
            try:
                pub_dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).astimezone(KST)
            except (ValueError, TypeError):
                pass
        if pub_dt is None:
            continue  # 발행일 없으면 스킵 (안전)

        # 매체명 추출
        source_name = ''
        if hasattr(entry, 'source') and entry.source:
            try:
                source_name = entry.source.get('title', '') or ''
            except AttributeError:
                source_name = getattr(entry.source, 'title', '') or ''
        if not source_name:
            source_name = getattr(entry, 'author', '') or '뉴스'

        # 요약 (HTML 태그 제거 + 길이 제한)
        raw_summary = entry.get('summary', '') or ''
        clean_summary = re.sub(r'<[^>]+>', '', raw_summary)
        clean_summary = re.sub(r'\s+', ' ', clean_summary).strip()[:250]

        items.append({
            'title': entry.get('title', '').strip(),
            'link': entry.get('link', '').strip(),
            'summary_raw': clean_summary,
            'source': source_name.strip(),
            'date': pub_dt.strftime('%Y-%m-%d'),
            'pub_dt': pub_dt,
        })
    return items


def collect_candidates() -> list[dict]:
    """카테고리별로 RSS 모아서, 최근 N일 내 후보 목록 반환."""
    cutoff = datetime.now(KST) - timedelta(days=DAYS_WINDOW)
    print(f'Fetching RSS feeds (최근 {DAYS_WINDOW}일 이내)…', flush=True)

    result = []
    for cat in CATEGORY_DEFS:
        all_items = []
        for q, lang in cat['queries']:
            url = google_news_rss_url(q, lang)
            items = fetch_rss_items(url)
            all_items.extend(items)

        # 3일 이내 필터
        recent = [i for i in all_items if i['pub_dt'] >= cutoff]

        # 제목 앞부분 기준 중복 제거 (다른 매체가 같은 사건 다룬 경우)
        seen = set()
        unique = []
        for item in sorted(recent, key=lambda x: x['pub_dt'], reverse=True):
            key = re.sub(r'\s+', ' ', item['title'])[:40]
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)

        top = unique[:8]  # Claude한테 최대 8개 후보 전달
        print(f'  {cat["title"]}: {len(top)} 후보 (전체 {len(all_items)} → 3일 이내 {len(recent)})', flush=True)

        result.append({
            'key': cat['key'],
            'title': cat['title'],
            'color': cat['color'],
            'candidates': top,
        })
    return result


# ----------------------------------------------------------------------------
# 2) Claude로 선정 + 한국어 요약 (web_search 없이 단순 텍스트 작업)
# ----------------------------------------------------------------------------

def curate_news() -> dict:
    candidates = collect_candidates()
    today_kor = kst_today_kor()

    # 후보 목록을 텍스트로 직렬화
    sections = []
    for cat in candidates:
        if not cat['candidates']:
            sections.append(f"\n## {cat['title']} (후보 0건 — 빈 카테고리로 출력)\n")
            continue
        section = f"\n## {cat['title']} (후보 {len(cat['candidates'])}건)\n"
        for i, item in enumerate(cat['candidates'], 1):
            section += f"\n[후보 {i}]\n"
            section += f"  date: {item['date']}\n"
            section += f"  title: {item['title']}\n"
            section += f"  source: {item['source']}\n"
            section += f"  url: {item['link']}\n"
            if item['summary_raw']:
                section += f"  context: {item['summary_raw'][:180]}\n"
        sections.append(section)

    prompt = f"""너는 한국어 뉴스 큐레이터다. 오늘은 {today_kor}.

아래 RSS 피드에서 자동 수집된 후보 기사들 중, 각 카테고리에서 가장 중요하고 흥미로운 **최대 3건**씩 골라 한국어 요약을 작성해 JSON으로 출력하라.

{''.join(sections)}

# 절대 규칙 (위반 = 작업 실패)
1. **title / source / date / url 은 위 후보 데이터에서 글자 그대로 복사**. 한 글자도 수정/생성 금지.
2. **summary 만 너가 한국어로 새로 작성** (50~120자, 1~2줄, 핵심만 간결히).
3. **후보가 3건 이상인 카테고리는 반드시 정확히 3건 선정**. 거르지 말 것.
4. 후보가 1~2건뿐이면 있는 만큼만 출력. 후보가 정말 0건이어야만 `"items": []`.
5. 모든 4개 카테고리(domestic, tech, ai, finance)를 정의된 순서대로 출력.
6. JSON 한 덩어리만 출력. 다른 설명/코드 펜스/사과문 금지.
7. 같은 사건 후속 보도가 한 카테고리에 몰리지 않게 분산. 분산 후에도 3건은 채울 것.

# 선정 기준 (참고용, 거르는 용도 X)
- 후보가 다 비슷비슷하면 그냥 가장 최신 3개 선택
- 거르는 게 망설여지면 무조건 포함 (기준이 모호하면 포함 쪽으로)

# 출력 JSON 스키마

{{
  "categories": [
    {{
      "key": "domestic",
      "title": "🌐 국내외 뉴스",
      "color": "#1a73e8",
      "items": [
        {{"title": "후보 그대로", "summary": "한국어 요약", "source": "후보 그대로", "date": "후보 그대로", "url": "후보 그대로"}}
      ]
    }},
    {{"key": "tech", "title": "💻 IT / 테크", "color": "#10b981", "items": [...]}},
    {{"key": "ai", "title": "🤖 AI", "color": "#8b5cf6", "items": [...]}},
    {{"key": "finance", "title": "💰 금융 / 경제", "color": "#f59e0b", "items": [...]}}
  ]
}}
"""

    client = anthropic.Anthropic()
    print('Calling Claude (Haiku) for curation…', flush=True)
    response = client.messages.create(
        model='claude-haiku-4-5-20251001',  # web_search 안 쓰니 Haiku로 충분
        max_tokens=3500,
        messages=[{'role': 'user', 'content': prompt}],
    )
    print(f'  stop_reason={response.stop_reason}', flush=True)

    text_blocks = [b for b in response.content if getattr(b, 'type', '') == 'text']
    if not text_blocks:
        raise RuntimeError('Claude 응답에 text 블록 없음')
    final_text = text_blocks[-1].text.strip()

    if final_text.startswith('```'):
        final_text = re.sub(r'^```(?:json)?\s*\n', '', final_text)
        final_text = re.sub(r'\n```\s*$', '', final_text)

    try:
        return json.loads(final_text)
    except json.JSONDecodeError:
        start = final_text.find('{')
        end = final_text.rfind('}')
        if start >= 0 and end > start:
            return json.loads(final_text[start:end + 1])
        raise RuntimeError(f'JSON 파싱 실패. 원본: {final_text[:500]}')


# ----------------------------------------------------------------------------
# 3) JSON → HTML 뉴스레터 (Gmail iOS 다크모드 최적화)
# ----------------------------------------------------------------------------

def html_escape(s: str) -> str:
    return (
        s.replace('&', '&amp;')
         .replace('<', '&lt;')
         .replace('>', '&gt;')
         .replace('"', '&quot;')
    )


def build_html(news_data: dict, today_kor: str) -> str:
    parts = [f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
<title>오늘의 뉴스레터</title>
</head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:-apple-system,BlinkMacSystemFont,'Apple SD Gothic Neo','Segoe UI',sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#f5f5f5" style="background:#f5f5f5;">
<tr><td align="center" style="padding:20px 10px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;width:100%;border-radius:12px;overflow:hidden;">
<tr>
<td bgcolor="#0f172a" style="background:#0f172a;padding:36px 24px;text-align:center;">
<p style="margin:0 0 8px;font-size:13px;color:#cbd5e1 !important;letter-spacing:1.5px;font-weight:500;">{html_escape(today_kor)}</p>
<h1 style="margin:0;font-size:28px;font-weight:800;color:#fbbf24 !important;line-height:1.3;">☀️ 오늘의 뉴스레터</h1>
<p style="margin:8px 0 0;font-size:14px;color:#e2e8f0 !important;">정열님의 아침 브리핑</p>
</td>
</tr>"""]

    for cat in news_data.get('categories', []):
        color = cat.get('color', '#1a73e8')
        title = html_escape(cat.get('title', ''))
        parts.append(f"""
<tr><td bgcolor="#ffffff" style="background:#ffffff;padding:24px 20px 8px;">
<h2 style="margin:0 0 16px;font-size:20px;font-weight:700;color:#0f172a;border-bottom:3px solid {color};padding-bottom:8px;display:inline-block;">{title}</h2>
</td></tr>""")
        items = cat.get('items', [])
        if not items:
            parts.append(f"""
<tr><td bgcolor="#ffffff" style="background:#ffffff;padding:0 20px 12px;">
<p style="margin:0;font-size:14px;color:#94a3b8;font-style:italic;">최근 3일 이내 해당 주제의 주요 뉴스가 없습니다.</p>
</td></tr>""")
        for item in items:
            t = html_escape(item.get('title', ''))
            s = html_escape(item.get('summary', ''))
            src = html_escape(item.get('source', ''))
            d = html_escape(item.get('date', ''))
            u = item.get('url', '#')
            parts.append(f"""
<tr><td bgcolor="#ffffff" style="background:#ffffff;padding:0 20px 12px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
<tr><td bgcolor="#f8fafc" style="background:#f8fafc;padding:16px;border-radius:8px;border-left:4px solid {color};">
<h3 style="margin:0 0 8px;font-size:16px;font-weight:700;color:#0f172a;line-height:1.4;">{t}</h3>
<p style="margin:0 0 10px;font-size:14px;color:#475569;line-height:1.6;">{s}</p>
<p style="margin:0;font-size:13px;color:#64748b;">{src} · {d} · <a href="{u}" style="color:{color};text-decoration:none;font-weight:600;">원문 보기 →</a></p>
</td></tr>
</table>
</td></tr>""")

    parts.append("""
<tr>
<td bgcolor="#0f172a" style="background:#0f172a;padding:24px 20px;text-align:center;">
<p style="margin:0;font-size:12px;color:#94a3b8 !important;">매일 아침 7시 · 정열님을 위한 큐레이션</p>
</td>
</tr>
</table>
</td></tr>
</table>
</body>
</html>""")
    return ''.join(parts)


# ----------------------------------------------------------------------------
# 4) Gmail SMTP 발송
# ----------------------------------------------------------------------------

def send_gmail(html_body: str, today_kor: str) -> None:
    user = os.environ['GMAIL_USER']
    password = os.environ['GMAIL_APP_PASSWORD'].replace(' ', '')

    msg = MIMEMultipart('alternative')
    msg['From'] = formataddr(('☀️ 뉴스레터', user))
    msg['To'] = user
    msg['Subject'] = f'☀️ 오늘의 뉴스레터 - {today_kor}'
    msg.attach(MIMEText(html_body, 'html', 'utf-8'))

    print('Sending Gmail via SMTP…', flush=True)
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL('smtp.gmail.com', 465, context=context) as server:
        server.login(user, password)
        server.send_message(msg)
    print('  ✅ Gmail 발송 완료', flush=True)


# ----------------------------------------------------------------------------
# 5) 카카오톡 "나에게 보내기" — 단순 알림 (버튼 없음)
# ----------------------------------------------------------------------------

def refresh_kakao_token() -> tuple[str, str | None]:
    rest_key = os.environ['KAKAO_REST_API_KEY']
    secret = os.environ['KAKAO_CLIENT_SECRET']
    refresh_token = os.environ['KAKAO_REFRESH_TOKEN']

    print('Refreshing Kakao token…', flush=True)
    resp = requests.post(
        'https://kauth.kakao.com/oauth/token',
        data={
            'grant_type': 'refresh_token',
            'client_id': rest_key,
            'client_secret': secret,
            'refresh_token': refresh_token,
        },
        timeout=15,
    )
    data = resp.json()
    if 'error' in data:
        raise RuntimeError(f'카카오 토큰 갱신 실패: {data}')
    access = data['access_token']
    new_refresh = data.get('refresh_token')
    if new_refresh:
        print(
            f'  ⚠️ Kakao refresh_token 이 회전됐습니다. '
            f'GitHub → Settings → Secrets → KAKAO_REFRESH_TOKEN 을 새 값으로 업데이트하세요. '
            f'(끝 4자리: …{new_refresh[-4:]})',
            flush=True,
        )
    return access, new_refresh


def send_kakao(message: str) -> None:
    access_token, _ = refresh_kakao_token()

    # 카카오 text 템플릿은 link 필드가 필수 (등록된 도메인이어야 함).
    # button_title 을 생략하면 버튼이 표시되지 않아 깔끔한 알림이 됨.
    template_object = {
        'object_type': 'text',
        'text': message,
        'link': {
            'web_url': 'https://jungyulcolinkim.github.io/Gmail-Landing/',
            'mobile_web_url': 'https://jungyulcolinkim.github.io/Gmail-Landing/',
        },
    }

    print('Sending KakaoTalk message…', flush=True)
    resp = requests.post(
        'https://kapi.kakao.com/v2/api/talk/memo/default/send',
        headers={'Authorization': f'Bearer {access_token}'},
        data={'template_object': json.dumps(template_object, ensure_ascii=False)},
        timeout=15,
    )
    result = resp.json()
    if result.get('result_code') != 0:
        raise RuntimeError(f'카카오 발송 실패: {result}')
    print('  ✅ 카카오톡 발송 완료', flush=True)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main() -> int:
    today_kor = kst_today_kor()
    print(f'\n📰 {today_kor} 뉴스레터 시작\n' + '=' * 50, flush=True)

    # 1. RSS 수집 + Claude 큐레이션
    try:
        news_data = curate_news()
        total = sum(len(c.get('items', [])) for c in news_data.get('categories', []))
        print(f'  ✅ 뉴스 {total}건 큐레이션 완료', flush=True)
    except Exception as e:
        print(f'  ❌ 뉴스 큐레이션 실패: {e}', flush=True)
        return 1

    # 1-b. 날짜 안전망 검증
    valid_from = (datetime.now(KST) - timedelta(days=DAYS_WINDOW)).strftime('%Y-%m-%d')
    valid_to = today_iso()
    out_of_range = []
    for cat in news_data.get('categories', []):
        for item in cat.get('items', []):
            d = item.get('date', '')
            if not re.match(r'^\d{4}-\d{2}-\d{2}$', d) or d < valid_from or d > valid_to:
                out_of_range.append((cat.get('title', ''), item.get('title', ''), d))
    if out_of_range:
        print(f'  ⚠️ 날짜 범위({valid_from} ~ {valid_to}) 벗어난 기사 {len(out_of_range)}건:', flush=True)
        for cat_title, item_title, d in out_of_range:
            print(f'     - [{cat_title}] {item_title} ({d})', flush=True)

    # 2. HTML 생성
    html = build_html(news_data, today_kor)
    print(f'  ✅ HTML 생성 완료 ({len(html):,} bytes)', flush=True)

    # 3. Gmail 발송
    gmail_ok = False
    try:
        send_gmail(html, today_kor)
        gmail_ok = True
    except Exception as e:
        print(f'  ❌ Gmail 실패: {e}', flush=True)

    # 4. 카카오톡 알림 (버튼 없는 단순 알림)
    kakao_ok = False
    try:
        kakao_msg = '☀️ 오늘의 뉴스레터가 도착했어요!\n\n📧 지메일 앱을 열어 확인해주세요.'
        send_kakao(kakao_msg)
        kakao_ok = True
    except Exception as e:
        print(f'  ❌ 카카오톡 실패: {e}', flush=True)

    print('\n' + '=' * 50)
    print(f'📧 Gmail: {"✅" if gmail_ok else "❌"}')
    print(f'💬 KakaoTalk: {"✅" if kakao_ok else "❌"}')
    print(f'📊 뉴스: {total}건')
    return 0 if (gmail_ok and kakao_ok) else 1


if __name__ == '__main__':
    sys.exit(main())
