import logging
import asyncio
import re
import string
from unidecode import unidecode
from telegram import Update, InputFile, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
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
- Phải có một tiêu đề 2 là "Kết luận" chỉ để mỗi tiêu đề đề Kết luận không thêm bất cứ gì thêm. Trong đoạn dẫn của kết luận có chứa từ khoá chính. Tóm tắt lại nội dung bài và nhấn mạnh thông điệp cuối cùng và không được chèn CTA.
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
# Lưu trữ tạm thời data để chờ user chọn featured image
temp_data = {}

def format_headings_and_keywords(html, keyword):
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
    slug_chars = []
    for c in text:
        if c in allowed:
            slug_chars.append(c)
        elif c in (' ', '_'):
            slug_chars.append('-')
    slug_text = ''.join(slug_chars)
    while '--' in slug_text:
        slug_text = slug_text.replace('--', '-')
    slug_text = slug_text.strip('-')
    return slug_text[:50] or 'image'

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
    caption_prompt = f"Viết caption ngắn gọn, súc tích dưới 120 ký tự cho ảnh minh họa phần {index} với nội dung sau: {prompt_text}"
    response = await openai_client.chat.completions.create(
        model="gpt-4.1-nano",
        messages=[{"role": "user", "content": caption_prompt}],
        temperature=0.7
    )
    return response.choices[0].message.content.strip()

def draw_caption_centered(draw, img_width, img_height, caption_text, font):
    max_width = int(img_width * 0.9)

    lines = []
    words = caption_text.split()
    line = ""
    for word in words:
        test_line = f"{line} {word}".strip()
        bbox = draw.textbbox((0, 0), test_line, font=font)
        w = bbox[2] - bbox[0]
        if w <= max_width:
            line = test_line
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)

    bbox = draw.textbbox((0, 0), "Ay", font=font)
    line_height = bbox[3] - bbox[1]
    total_height = line_height * len(lines)

    y_start = img_height - total_height - 10

    for i, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font)
        w = bbox[2] - bbox[0]
        x = (img_width - w) // 2
        y = y_start + i * line_height

        for dx in range(-2, 3):
            for dy in range(-2, 3):
                if dx != 0 or dy != 0:
                    draw.text((x + dx, y + dy), line, font=font, fill="black")
        draw.text((x, y), line, font=font, fill="white")

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
        logging.error(f"Load font lỗi: {e}, fallback font default")
        font = ImageFont.load_default()

    draw_caption_centered(draw, img.width, img.height, caption_text, font)

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

    slug = to_slug(caption_text)
    filepath = f"/tmp/{slug}.jpg"
    with open(filepath, 'wb') as f:
        f.write(buffer.getvalue())

    return filepath, slug

def upload_image_to_wordpress(filepath, slug, alt, caption):
    """Upload ảnh lên WordPress và trả về URL + attachment ID"""
    with open(filepath, 'rb') as img_file:
        data = {
            'name': f"{slug}.jpg",
            'type': 'image/jpeg',
            'bits': xmlrpc_client.Binary(img_file.read()),
        }
    response = wp_client.call(UploadFile(data))
    attachment_url = response['url']
    attachment_id = response['id']  # Lấy attachment ID để set featured image
    return attachment_url, attachment_id

def insert_images_in_content(content, image_urls, alts, captions):
    parts = content.split('\n')
    n = len(parts)

    figure_template = lambda url, alt, cap: f'''
<figure>
  <img src="{url}" alt="{alt}" width="800" height="400"/>
  <figcaption>{cap}</figcaption>
</figure>'''

    parts.insert(1, figure_template(image_urls[0], alts[0], captions[0]))
    parts.insert(n//2, figure_template(image_urls[1], alts[1], captions[1]))
    parts.insert(n-2, figure_template(image_urls[2], alts[2], captions[2]))

    return '\n'.join(parts)

def remove_hr_after_post(post_id):
    post = wp_client.call(GetPost(post_id))
    content = post.content
    # Xoá thẻ <hr /> và các dòng trắng xung quanh nó sau khi đăng
    content = re.sub(r'\n*\s*<hr\s*/?>\s*\n*', '\n', content, flags=re.IGNORECASE)
    post.content = content
    wp_client.call(EditPost(post_id, post))

def set_featured_image(post_id, attachment_id):
    """Set ảnh đại diện cho bài viết"""
    try:
        # Sử dụng custom field để set featured image
        post = wp_client.call(GetPost(post_id))
        
        # Thêm custom field _thumbnail_id
        if not hasattr(post, 'custom_fields') or post.custom_fields is None:
            post.custom_fields = []
        
        # Tìm và cập nhật hoặc thêm mới _thumbnail_id
        thumbnail_field_exists = False
        for field in post.custom_fields:
            if field['key'] == '_thumbnail_id':
                field['value'] = str(attachment_id)
                thumbnail_field_exists = True
                break
        
        if not thumbnail_field_exists:
            post.custom_fields.append({
                'key': '_thumbnail_id',
                'value': str(attachment_id)
            })
        
        # Update post với custom field mới
        wp_client.call(EditPost(post_id, post))
        logging.info(f"✅ Đã set ảnh đại diện (ID: {attachment_id}) cho bài viết ID: {post_id}")
        
    except Exception as e:
        logging.error(f"❌ Lỗi khi set featured image: {str(e)}")

async def show_image_selection(context, chat_id, keyword, article_data, image_data_list):
    """Hiển thị 3 ảnh và cho user chọn ảnh đại diện"""
    
    # Lưu data tạm thời
    temp_key = f"{chat_id}_{keyword}"
    temp_data[temp_key] = {
        'keyword': keyword,
        'article_data': article_data,
        'image_data_list': image_data_list,
        'chat_id': chat_id
    }
    
    # Tạo inline keyboard với 3 nút chọn ảnh
    keyboard = [
        [InlineKeyboardButton(f"🖼️ Chọn ảnh 1", callback_data=f"select_image_{temp_key}_0")],
        [InlineKeyboardButton(f"🖼️ Chọn ảnh 2", callback_data=f"select_image_{temp_key}_1")],
        [InlineKeyboardButton(f"🖼️ Chọn ảnh 3", callback_data=f"select_image_{temp_key}_2")],
        [InlineKeyboardButton(f"🚀 Đăng bài không ảnh đại diện", callback_data=f"select_image_{temp_key}_none")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Gửi 3 ảnh cho user xem
    message = f"📸 **Chọn ảnh đại diện cho bài viết: {keyword}**\n\n"
    await context.bot.send_message(chat_id=chat_id, text=message)
    
    for i, (url, attachment_id, alt, caption) in enumerate(image_data_list, 1):
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=url,
            caption=f"**Ảnh {i}:** {caption}"
        )
    
    await context.bot.send_message(
        chat_id=chat_id,
        text="👆 Chọn ảnh nào làm ảnh đại diện cho bài viết:",
        reply_markup=reply_markup
    )

async def handle_image_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xử lý khi user chọn ảnh đại diện"""
    query = update.callback_query
    await query.answer()
    
    callback_data = query.data
    if not callback_data.startswith("select_image_"):
        return
    
    # Parse callback data: select_image_{chat_id}_{keyword}_{image_index}
    parts = callback_data.split("_")
    if len(parts) < 4:
        await query.edit_message_text("❌ Lỗi dữ liệu callback")
        return
    
    temp_key = "_".join(parts[2:-1])  # chat_id_keyword
    selected_index = parts[-1]  # image index hoặc "none"
    
    if temp_key not in temp_data:
        await query.edit_message_text("❌ Dữ liệu đã hết hạn. Vui lòng thử lại.")
        return
    
    data = temp_data[temp_key]
    keyword = data['keyword']
    article_data = data['article_data']
    image_data_list = data['image_data_list']
    chat_id = data['chat_id']
    
    try:
        await query.edit_message_text(f"⏳ Đang đăng bài viết: {keyword}...")
        
        if selected_index == "none":
            # Đăng bài không ảnh đại diện
            link = post_to_wordpress(keyword, article_data, image_data_list, featured_image_index=None)
            message = f"✅ Đăng thành công: {link}\n🖼️ Không có ảnh đại diện"
        else:
            # Đăng bài với ảnh đại diện được chọn
            selected_idx = int(selected_index)
            link = post_to_wordpress(keyword, article_data, image_data_list, featured_image_index=selected_idx)
            selected_caption = image_data_list[selected_idx][3]
            message = f"✅ Đăng thành công: {link}\n🖼️ Ảnh đại diện: {selected_caption}"
        
        results.append([len(results) + 1, keyword, link])
        await context.bot.send_message(chat_id=chat_id, text=message)
        
        # Xóa data tạm thời
        del temp_data[temp_key]
        
    except Exception as e:
        await context.bot.send_message(
            chat_id=chat_id, 
            text=f"❌ Lỗi khi đăng bài {keyword}: {str(e)}"
        )
        if temp_key in temp_data:
            del temp_data[temp_key]
    """
    image_data_list: danh sách chứa (url, attachment_id, alt, caption) cho mỗi ảnh
    """
    image_urls = [data[0] for data in image_data_list]
    alts = [data[2] for data in image_data_list]
    captions = [data[3] for data in image_data_list]
    
    content_with_images = insert_images_in_content(article_data["content"], image_urls, alts, captions)

    html = markdown2.markdown(content_with_images)
    html = format_headings_and_keywords(html, keyword)

    post = WordPressPost()
    post.title = article_data["post_title"]
    post.content = str(html)
    post.post_status = 'publish'
    post.slug = to_slug(keyword)

    post.custom_fields = [
        {'key': 'rank_math_title', 'value': article_data["meta_title"]},
        {'key': 'rank_math_description', 'value': article_data["meta_description"]},
        {'key': 'rank_math_focus_keyword', 'value': article_data["focus_keyword"]},
        {'key': 'rank_math_keywords', 'value': article_data["focus_keyword"]}
    ]

    post_id = wp_client.call(NewPost(post))

    # Set ảnh đầu tiên làm featured image
    first_image_attachment_id = image_data_list[0][1]
    set_featured_image(post_id, first_image_attachment_id)

    # Xoá hr sau khi đăng bài
    remove_hr_after_post(post_id)

    return f"{WORDPRESS_URL}/{post.slug}/"

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

        image_data_list = []  # Lưu (url, attachment_id, alt, caption)

        for i, prompt_text in enumerate(image_prompts, 1):
            filepath, slug = await create_and_process_image(prompt_text, keyword, i, image_captions[i-1])
            alt_text = image_captions[i-1]
            url, attachment_id = upload_image_to_wordpress(filepath, slug, alt_text, image_captions[i-1])
            image_data_list.append((url, attachment_id, alt_text, image_captions[i-1]))
            
            # Thông báo progress
            await context.bot.send_message(
                chat_id=context._chat_id, 
                text=f"📸 Đã tạo và upload ảnh {i}/3"
            )

        # Hiển thị ảnh để user chọn ảnh đại diện
        await show_image_selection(context, context._chat_id, keyword, article_data, image_data_list)
        
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
    
    # Xử lý từng keyword một
    while not keywords_queue.empty():
        keyword = await keywords_queue.get()
        await process_keyword(keyword, context)
        
    # Chờ user chọn hết ảnh rồi mới gửi report
    await update.message.reply_text("⏳ Đang chờ bạn chọn ảnh đại diện cho các bài viết...")

async def send_final_report(context, chat_id):
    """Gửi report cuối cùng sau khi tất cả bài viết đã được đăng"""
    if results:
        await write_report_and_send_to_chat(context, chat_id)
        await context.bot.send_message(
            chat_id=chat_id, 
            text=f"🎉 Hoàn thành! Đã đăng {len(results)} bài viết."
        )

async def write_report_and_send_to_chat(context, chat_id):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["STT", "Keyword", "Link đăng bài"])
    for row in results:
        sheet.append(row)
    filepath = "/tmp/report.xlsx"
    workbook.save(filepath)
    await context.bot.send_document(chat_id=chat_id, document=InputFile(filepath))

async def handle_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Vui lòng nhập từ khóa. Ví dụ: /keyword marketing online")
        return
    keyword = ' '.join(context.args)
    await process_keyword(keyword, context)

app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
app.add_handler(MessageHandler(filters.Document.ALL, handle_txt_file))
app.add_handler(CommandHandler("keyword", handle_keyword))
app.add_handler(CallbackQueryHandler(handle_image_selection))  # Thêm handler cho callback

if __name__ == '__main__':
    print("Bot is running...")
    app.run_polling()
