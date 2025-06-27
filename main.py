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
from wordpress_xmlrpc.methods.media import UploadFile
from wordpress_xmlrpc.compat import xmlrpc_client
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO

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

logging.basicConfig(level=logging.INFO)
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
wp_client = Client(WORDPRESS_URL, WORDPRESS_USER, WORDPRESS_PASS)
keywords_queue = asyncio.Queue()
results = []

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
    h1_match = re.search(r'#\s*(.*?)\n', raw)

    meta_title = meta_title_match.group(1).strip() if meta_title_match else keyword
    meta_description = meta_description_match.group(1).strip() if meta_description_match else ""
    h1_title = h1_match.group(1).strip() if h1_match else keyword

    content_start = h1_match.end() if h1_match else 0
    content = raw[content_start:].strip()

    return {
        "post_title": h1_title,
        "meta_title": meta_title,
        "meta_description": meta_description,
        "focus_keyword": keyword,
        "content": content
    }

async def split_content_into_three_parts(content):
    lines = content.split('\n')
    n = len(lines)
    part1 = '\n'.join(lines[: n//3])
    part2 = '\n'.join(lines[n//3: 2*n//3])
    part3 = '\n'.join(lines[2*n//3 :])
    return part1, part2, part3

async def generate_caption(prompt_text, index):
    caption_prompt = f"Viết caption ngắn gọn, súc tích cho ảnh minh họa phần {index} với nội dung sau: {prompt_text}"
    response = await openai_client.chat.completions.create(
        model="gpt-4.1-nano",
        messages=[{"role": "user", "content": caption_prompt}],
        temperature=0.7
    )
    return response.choices[0].message.content.strip()

async def create_and_process_image(prompt_text, keyword, index, caption_text):
    response = await openai_client.images.generate(
        model="dall-e-3",
        prompt=prompt_text,
        n=1,
        size="1024x1024"
    )
    img_url = response.data[0].url

    async with aiohttp.ClientSession() as session:
        async with session.get(img_url) as resp:
            img_bytes = await resp.read()

    img = Image.open(BytesIO(img_bytes)).convert('RGB')
    img = img.resize((800, 400))

    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 16)
    except:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), caption_text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    x = 10
    y = img.height - text_height - 10
    draw.rectangle([x - 5, y - 5, x + text_width + 5, y + text_height + 5], fill=(0, 0, 0, 128))
    draw.text((x, y), caption_text, font=font, fill=(255, 255, 255))

    quality = 85
    buffer = BytesIO()
    while True:
        buffer.seek(0)
        buffer.truncate()
        img.save(buffer, format='JPEG', quality=quality)
        size_kb = buffer.tell() / 1024
        if size_kb <= 100 or quality <= 30:
            break
        quality -= 5

    slug = f"anh-mo-ta-{keyword.replace(' ', '-').lower()}-{index}"
    filepath = f"/tmp/{slug}.jpg"
    with open(filepath, 'wb') as f:
        f.write(buffer.getvalue())

    return filepath, slug

def upload_image_to_wordpress(filepath, slug, alt, caption):
    with open(filepath, 'rb') as img_file:
        data = {
            'name': f"{slug}.jpg",
            'type': 'image/jpeg',
            'bits': xmlrpc_client.Binary(img_file.read()),
        }
    response = wp_client.call(UploadFile(data))
    attachment_url = response['url']
    return attachment_url

def insert_images_in_content(content, image_urls, alts, captions):
    parts = content.split('\n')
    n = len(parts)

    figure_template = lambda url, alt, cap: f'''
<figure>
  <img src="{url}" alt="{alt}" width="800" height="400"/>
  <figcaption>{cap}</figcaption>
</figure>'''

    parts.insert(1, figure_template(image_urls[0], alts[0], captions[0]))  # Đầu bài
    parts.insert(n//2, figure_template(image_urls[1], alts[1], captions[1]))  # Giữa bài
    parts.insert(n-2, figure_template(image_urls[2], alts[2], captions[2]))  # Gần cuối bài

    return '\n'.join(parts)

def post_to_wordpress(keyword, article_data, image_urls, alts, captions):
    content_with_images = insert_images_in_content(article_data["content"], image_urls, alts, captions)
    html = markdown2.markdown(content_with_images)
    html = format_headings_and_keywords(html, keyword)

    post = WordPressPost()
    post.title = article_data["post_title"]
    post.content = str(html)
    post.post_status = 'publish'

    post.custom_fields = [
        {'key': 'rank_math_title', 'value': article_data["meta_title"]},
        {'key': 'rank_math_description', 'value': article_data["meta_description"]},
        {'key': 'rank_math_focus_keyword', 'value': article_data["focus_keyword"]},
        {'key': 'rank_math_keywords', 'value': article_data["focus_keyword"]}
    ]

    post_id = wp_client.call(NewPost(post))
    return f"{WORDPRESS_URL}/?p={post_id}"

async def process_keyword(keyword, context):
    await context.bot.send_message(chat_id=context._chat_id, text=f"🔄 Đang xử lý từ khóa: {keyword}")
    try:
        article_data = await generate_article(keyword)
        part1, part2, part3 = await split_content_into_three_parts(article_data["content"])

        image_prompts = [
            f"Ảnh minh họa nội dung đầu bài viết, phong cách đơn giản, tươi sáng không nhạy cảm và phản cảm: {part1[:200]}",
            f"Ảnh minh họa nội dung giữa bài viết, phong cách đơn giản, tươi sáng không nhạy cảm và phản cảm: {part2[:200]}",
            f"Ảnh minh họa nội dung cuối bài viết, phong cách đơn giản, tươi sáng không nhạy cảm và phản cảm: {part3[:200]}"
        ]

        image_captions = []
        for i, prompt_text in enumerate(image_prompts, 1):
            caption = await generate_caption(prompt_text, i)
            image_captions.append(caption)

        image_urls = []
        alts = []
        captions = []

        for i, prompt_text in enumerate(image_prompts, 1):
            filepath, slug = await create_and_process_image(prompt_text, keyword, i, image_captions[i-1])
            alt_text = f"Ảnh minh họa phần {i} bài viết"
            url = upload_image_to_wordpress(filepath, slug, alt_text, image_captions[i-1])
            image_urls.append(url)
            alts.append(alt_text)
            captions.append(image_captions[i-1])

        link = post_to_wordpress(keyword, article_data, image_urls, alts, captions)
        results.append([len(results) + 1, keyword, link])
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
