import logging
import asyncio
import re
import string
from unidecode import unidecode
from telegram import Update, InputFile
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from openai import AsyncOpenAI
import aiohttp
import aiofiles
import os
import openpyxl
import markdown2
from wordpress_xmlrpc import Client, WordPressPost
from wordpress_xmlrpc.methods.posts import NewPost, GetPost, EditPost
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

FONT_PATH = os.path.join(os.path.dirname(__file__), "NotoSans-Regular.ttf")

SEO_PROMPT = '''Bạn là một chuyên gia viết nội dung SEO. Viết một bài blog dài khoảng 3500 từ chuẩn SEO và độ unique cao có sự khác biệt hơn với các bài viết trước đó với từ khóa chính là: "{keyword}".
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
    # In đậm từ khóa trong các tiêu đề và nội dung
    for tag in ['h1', 'h2', 'h3', 'h4']:
        pattern = fr'<{tag}>(.*?)</{tag}>'
        repl = fr'<{tag}><strong>\1</strong></{tag}>'
        html = re.sub(pattern, repl, html, flags=re.DOTALL)
    html = re.sub(re.escape(keyword), fr'<strong>{keyword}</strong>', html, flags=re.IGNORECASE)
    return html

def to_slug(text):
    # Chuyển tiếng Việt sang ASCII chuẩn
    text = unidecode(text)
    text = text.lower()
    allowed = string.ascii_lowercase + string.digits + '-'
    slug_chars = [c if c in allowed else '-' for c in text]
    return ''.join(slug_chars).strip('-')[:50] or 'image'

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

    meta_title = re.search(r"(?i)^1\..*?Meta Title.*?:\s*(.*)", raw, re.MULTILINE)
    meta_description = re.search(r"(?i)^2\..*?Meta Description.*?:\s*(.*)", raw, re.MULTILINE)
    h1_title = re.search(r'#\s*(.*?)\n', raw)

    return {
        "post_title": h1_title.group(1).strip() if h1_title else keyword,
        "meta_title": meta_title.group(1).strip() if meta_title else keyword,
        "meta_description": meta_description.group(1).strip() if meta_description else "",
        "content": raw[content_start:].strip()
    }

async def generate_caption(prompt_text, index):
    caption_prompt = f"Viết caption ngắn gọn, súc tích dưới 120 ký tự cho ảnh minh họa phần {index}: {prompt_text}"
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
        font = ImageFont.truetype(FONT_PATH, 28)
    except Exception as e:
        logging.error(f"Load font lỗi: {e}")
        font = ImageFont.load_default()

    draw_caption_centered(draw, img.width, img.height, caption_text, font)

    buffer = BytesIO()
    img.save(buffer, format='JPEG', quality=85)
    buffer.seek(0)
    slug = to_slug(caption_text)
    filepath = f"/tmp/{slug}.jpg"
    with open(filepath, 'wb') as f:
        f.write(buffer.getvalue())

    return filepath, slug

async def upload_image_to_wordpress(filepath, slug, caption):
    with open(filepath, 'rb') as img_file:
        data = {
            'name': f"{slug}.jpg",
            'type': 'image/jpeg',
            'bits': xmlrpc_client.Binary(img_file.read()),
        }
    response = wp_client.call(UploadFile(data))
    return response['url']

def insert_images_in_content(content, image_urls, captions):
    parts = content.split('\n')
    figure_template = lambda url, cap: f'<figure><img src="{url}" alt="{cap}" width="800" height="400"/><figcaption>{cap}</figcaption></figure>'
    
    parts.insert(1, figure_template(image_urls[0], captions[0]))
    parts.insert(len(parts)//2, figure_template(image_urls[1], captions[1]))
    parts.insert(len(parts)-2, figure_template(image_urls[2], captions[2]))

    return '\n'.join(parts)

async def process_keyword(keyword, context):
    await context.bot.send_message(chat_id=context._chat_id, text=f"🔄 Đang xử lý từ khóa: {keyword}")
    try:
        article_data = await generate_article(keyword)
        part1, part2, part3 = await split_content_into_three_parts(article_data["content"])

        image_prompts = [
            f"Ảnh minh họa đầu bài: {part1[:200]}",
            f"Ảnh minh họa giữa bài: {part2[:200]}",
            f"Ảnh minh họa cuối bài: {part3[:200]}"
        ]

        image_captions = [await generate_caption(prompt, i) for i, prompt in enumerate(image_prompts, 1)]
        image_urls = [await upload_image_to_wordpress(await create_and_process_image(prompt, keyword, i, caption)[0], to_slug(caption), caption) for i, (prompt, caption) in enumerate(zip(image_prompts, image_captions), 1)]
        
        content_with_images = insert_images_in_content(article_data["content"], image_urls, image_captions)

        post = WordPressPost()
        post.title = article_data["post_title"]
        post.content = markdown2.markdown(content_with_images)
        post.slug = to_slug(keyword)
        post.post_status = 'publish'
        post.custom_fields = [
            {'key': 'rank_math_title', 'value': article_data["meta_title"]},
            {'key': 'rank_math_description', 'value': article_data["meta_description"]},
            {'key': 'rank_math_focus_keyword', 'value': keyword}
        ]
        
        post_id = wp_client.call(NewPost(post))
        await context.bot.send_message(chat_id=context._chat_id, text=f"✅ Đăng thành công: {WORDPRESS_URL}/{post.slug}/")
    except Exception as e:
        await context.bot.send_message(chat_id=context._chat_id, text=f"❌ Lỗi với từ khóa {keyword}: {str(e)}")

async def handle_txt_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc.file_name.endswith(".txt"):
        await update.message.reply_text("❌ Vui lòng gửi file .txt chứa danh sách từ khóa.")
        return
    file = await context.bot.get_file(doc.file_id)
    path = f"/tmp/{doc.file_name}"
    await file.download_to_drive(path)

    async with aiofiles.open(path, mode='r') as f:
        keywords = [line.strip() for line in await f.readlines() if line.strip()]
        for keyword in keywords:
            await keywords_queue.put(keyword)
    
    await update.message.reply_text("📥 Đã nhận file. Bắt đầu xử lý...")

    await asyncio.gather(*(process_keyword(keyword, context) for keyword in keywords))
    await write_report_and_send(context)

app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
app.add_handler(MessageHandler(filters.Document.ALL, handle_txt_file))

if __name__ == '__main__':
    app.run_polling()
