# main.py
import logging
import asyncio
import re
from telegram import Update, InputFile
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from openai import AsyncOpenAI
import aiohttp
import aiofiles
import os
import openpyxl
import markdown2
from wordpress_xmlrpc import Client, WordPressPost
from wordpress_xmlrpc.methods.posts import NewPost

# --- Config ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WORDPRESS_URL = os.getenv("WORDPRESS_URL")
WORDPRESS_USER = os.getenv("WORDPRESS_USER")
WORDPRESS_PASS = os.getenv("WORDPRESS_PASS")

SEO_PROMPT = '''Bạn là một chuyên gia viết nội dung SEO. Viết một bài blog dài khoảng 2500 từ chuẩn SEO với từ khóa chính là: "{keyword}".
Yêu cầu cụ thể như sau:
---
1. Tiêu đề SEO (Meta Title):
- Chứa từ khóa chính
- Dưới 60 ký tự
- Phản ánh đúng mục đích tìm kiếm (search intent) của người dùng
2. Meta Description:
- Dài 150–160 ký tự
- Chứa từ khóa chính
- Tóm tắt đúng nội dung bài viết và thu hút người dùng click
---
3. Cấu trúc bài viết:
- Chỉ có 1 thẻ H1 duy nhất:
- Dưới 70 ký tự
- Chứa từ khóa chính
- Diễn tả bao quát toàn bộ chủ đề bài viết
- Sapo mở đầu ngay sau H1:
- Bắt đầu bằng từ khóa chính
- Dài từ 250–350 ký tự
- Viết theo kiểu gợi mở, đặt câu hỏi hoặc khơi gợi insight người tìm kiếm
- Tránh viết khô khan hoặc như mô tả kỹ thuật
- Tôi không cần bạn phải ghi rõ là Sapo:. Tôi là một SEO nên tôi đã biết rồi.
---
4. Thân bài:
- Có ít nhất 4 tiêu đề H2 (phải chứa từ khóa chính)
- Mỗi tiêu đề H2 gồm 2 đến 3 tiêu đề H3 bổ trợ
- H3 cũng nên chứa từ khóa chính hoặc biến thể của từ khóa
- Nếu phù hợp, có thể sử dụng thẻ H4 để phân tích chuyên sâu hơn
- Mỗi tiêu đề H2/H3 cần có một đoạn dẫn ngắn gợi mở nội dung
- Phải có một tiêu đề 2 là “Kết luận” chỉ để mỗi tiêu đề đề Kết luận không thêm bất cứ gì thêm. Trong đoạn dẫn của kết luận có chứa từ khoá chính. Tóm tắt lại nội dung bài và nhấn mạnh thông điệp cuối cùng và không được chèn CTA.
---
5. Tối ưu từ khóa:
- Mật độ từ khóa chính: 1% đến 1,5% cho một bài viết 1500 từ
- Phân bố đều ở sapo, H2, H3, thân bài, kết luận
- Tự nhiên, không nhồi nhét
- Thêm 3 ba từ khoá tự phụ ngữ nghĩa để bổ trợ
- In đậm từ khóa chính.
---
⚠️ Lưu ý: Viết bằng tiếng Việt, giọng văn rõ ràng, dễ hiểu, không lan man. Ưu tiên thông tin hữu ích, ví dụ thực tế, và có chiều sâu để tăng điểm chuyên môn với Google. Ngoài ra, các tiêu đề không được làm dạng bullet chỉ cần có định dạng tiêu đề là được rồi. Không cần phải có những thông tin lưu ý và câu hỏi mở rộng gì, thứ tôi cần chỉ là một bài content chuẩn seo'''

# --- Setup ---
logging.basicConfig(level=logging.INFO)
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
wp_client = Client(WORDPRESS_URL, WORDPRESS_USER, WORDPRESS_PASS)
keywords_queue = asyncio.Queue()
results = []

# --- Helpers ---
def format_headings_and_keywords(html, keyword):
    for tag in ['h1', 'h2', 'h3', 'h4']:
        pattern = fr'<{tag}>(.*?)</{tag}>'
        repl = fr'<{tag}><strong>\1</strong></{tag}>'
        html = re.sub(pattern, repl, html, flags=re.DOTALL)
    html = re.sub(re.escape(keyword), fr'<strong>{keyword}</strong>', html, flags=re.IGNORECASE)
    return html

async def generate_article(keyword):
    system_prompt = SEO_PROMPT.format(keyword=keyword)
    response = await openai_client.chat.completions.create(
        model="gpt-4.1-nano",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Từ khóa chính: {keyword}"}
        ],
        temperature=0.7
    )
    raw = response.choices[0].message.content.replace('—', '<hr>')
    raw = re.sub(r'(?i)^\s*Sapo:\s*\n?', '', raw, flags=re.MULTILINE)

    meta_title_match = re.search(r"(?i)^1\..*?Meta Title.*?:\s*(.*)", raw, re.MULTILINE)
    meta_description_match = re.search(r"(?i)^2\..*?Meta Description.*?:\s*(.*)", raw, re.MULTILINE)

    meta_title = meta_title_match.group(1).strip() if meta_title_match else keyword
    meta_description = meta_description_match.group(1).strip() if meta_description_match else ""

    content_start = re.search(r"(?i)^3\..*?Cấu trúc bài viết", raw)
    content = raw[content_start.start():] if content_start else raw

    return {
        "meta_title": meta_title,
        "meta_description": meta_description,
        "content": content
    }

def post_to_wordpress(keyword, article_data):
    html = markdown2.markdown(article_data["content"])
    html = format_headings_and_keywords(html, keyword)

    post = WordPressPost()
    post.title = article_data["meta_title"]
    post.content = str(html)
    post.post_status = 'publish'

    post.custom_fields = [
        {'key': 'rank_math_description', 'value': article_data["meta_description"]},
        {'key': 'rank_math_title', 'value': article_data["meta_title"]},
    ]

    post_id = wp_client.call(NewPost(post))
    return f"{WORDPRESS_URL}/?p={post_id}"

async def process_keyword(keyword, context):
    await context.bot.send_message(chat_id=context._chat_id, text=f"🔄 Đang xử lý từ khóa: {keyword}")
    try:
        article_data = await generate_article(keyword)
        link = post_to_wordpress(keyword, article_data)
        results.append([len(results)+1, keyword, link])
        await context.bot.send_message(chat_id=context._chat_id, text=f"✅ Đăng thành công: {link}")
    except Exception as e:
        await context.bot.send_message(chat_id=context._chat_id, text=f"❌ Lỗi với từ khóa {keyword}: {str(e)}")

async def write_report_and_send(context):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["STT", "Keyword", "Link đăng bài"])
    for row in results:
        sheet.append(row)
    filepath = "/tmp/report.xlsx"
    workbook.save(filepath)
    await context.bot.send_document(chat_id=context._chat_id, document=InputFile(filepath))

# --- Handlers ---
async def handle_txt_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc.file_name.endswith(".txt"):
        await update.message.reply_text("❌ Vui lòng gửi file .txt chứa danh sách từ khóa.")
        return
    file = await context.bot.get_file(doc.file_id)
    path = f"/tmp/{doc.file_name}"
    await file.download_to_drive(path)
    async with aiofiles.open(path, mode='r') as f:
        async for line in f:
            keyword = line.strip()
            if keyword:
                await keywords_queue.put(keyword)
    await update.message.reply_text("📥 Đã nhận file. Bắt đầu xử lý...")
    while not keywords_queue.empty():
        keyword = await keywords_queue.get()
        await process_keyword(keyword, context)
    await write_report_and_send(context)

async def handle_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Vui lòng nhập từ khóa. Ví dụ: /keyword marketing online")
        return
    keyword = ' '.join(context.args)
    await process_keyword(keyword, context)
    await write_report_and_send(context)

# --- Main ---
app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
app.add_handler(MessageHandler(filters.Document.ALL, handle_txt_file))
app.add_handler(CommandHandler("keyword", handle_keyword))

if __name__ == '__main__':
    print("Bot is running...")
    app.run_polling()
