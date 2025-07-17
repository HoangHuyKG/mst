from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright
from pdfminer.high_level import extract_text
from pdfminer.layout import LAParams
import asyncio
import os
import uuid
import logging
import time
import requests
import re
import pyodbc
from datetime import datetime
import json
from dotenv import load_dotenv
from supabase import create_client, Client
import psycopg2
from psycopg2.extras import RealDictCursor
import psycopg2.pool
import urllib.parse
load_dotenv()  # Thêm dòng này sau các import

# Cấu hình logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI()

# Configuration
CAPTCHA_API_KEY = os.environ.get('CAPTCHA_API_KEY', 'your_default_key')
TARGET_URL = "https://dangkyquamang.dkkd.gov.vn/egazette/Forms/Egazette/ANNOUNCEMENTSListingInsUpd.aspx"
SITE_KEY = "6LewYU4UAAAAAD9dQ51Cj_A_1uHLOXw9wJIxi9x0"

# Thay thế SQL_SERVER_CONFIG
SUPABASE_CONFIG = {
    'url': os.environ.get('SUPABASE_URL', 'your_supabase_url'),
    'key': os.environ.get('SUPABASE_SERVICE_ROLE_KEY', 'your_supabase_anon_key'),
    'database_url': os.environ.get('SUPABASE_DB_URL', 'postgresql://user:password@host:port/database')
}

class CaptchaSolver:
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "http://2captcha.com"
    
    def solve_recaptcha(self, sitekey, url):
        # Submit captcha
        submit_data = {
            'key': self.api_key,
            'method': 'userrecaptcha',
            'googlekey': sitekey,
            'pageurl': url,
            'json': 1
        }
        
        logger.info("Submitting captcha...")
        response = requests.post(f"{self.base_url}/in.php", data=submit_data, timeout=30)
        
        # Parse submit response
        try:
            if response.headers.get('content-type', '').startswith('application/json'):
                result = response.json()
                if result.get('status') == 1:
                    captcha_id = result['request']
                else:
                    raise Exception(f"Submit failed: {result}")
            else:
                if response.text.startswith('OK|'):
                    captcha_id = response.text.split('|')[1]
                else:
                    raise Exception(f"Submit failed: {response.text}")
        except Exception as e:
            raise Exception(f"Submit parsing error: {e}")
        
        logger.info(f"Captcha ID: {captcha_id}")
        
        # Get result
        for attempt in range(30):
            logger.info(f"Checking result {attempt + 1}/30")
            time.sleep(5)
            
            params = {'key': self.api_key, 'action': 'get', 'id': captcha_id, 'json': 1}
            response = requests.get(f"{self.base_url}/res.php", params=params, timeout=30)
            
            # Parse result response
            try:
                if response.headers.get('content-type', '').startswith('application/json'):
                    result = response.json()
                    if result.get('status') == 1:
                        return result['request']
                    elif result.get('error_text') == 'CAPCHA_NOT_READY':
                        continue
                    else:
                        raise Exception(f"Solve failed: {result}")
                else:
                    if response.text.startswith('OK|'):
                        return response.text.split('|')[1]
                    elif response.text == 'CAPCHA_NOT_READY':
                        continue
                    else:
                        raise Exception(f"Solve failed: {response.text}")
            except Exception as e:
                if 'CAPCHA_NOT_READY' in str(e):
                    continue
                raise Exception(f"Result parsing error: {e}")
        
        raise Exception("Timeout waiting for captcha solution")
    
    def get_balance(self):
        params = {'key': self.api_key, 'action': 'getbalance', 'json': 1}
        response = requests.get(f"{self.base_url}/res.php", params=params, timeout=30)
        
        try:
            if response.headers.get('content-type', '').startswith('application/json'):
                result = response.json()
                if result.get('status') == 1:
                    return float(result['request'])
                else:
                    raise Exception(f"Balance check failed: {result}")
            else:
                return float(response.text)
        except ValueError:
            raise Exception(f"Invalid balance response: {response.text}")
        
class DatabaseManager:
    def __init__(self, config):
        self.config = config
        self.supabase: Client = create_client(config['url'], config['key'])
        self.max_retries = 3
        self.retry_delay = 5
        
    def get_connection(self):
        """Tạo kết nối PostgreSQL"""
        try:
            conn = psycopg2.connect(
                self.config['database_url'],
                cursor_factory=RealDictCursor
            )
            conn.autocommit = False
            return conn
        except Exception as e:
            logger.error(f"Database connection failed: {e}")
            raise
    
    def create_tables(self):
        """Tạo bảng PostgreSQL"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                create_table_query = """
                CREATE TABLE IF NOT EXISTS company_info (
                    id SERIAL PRIMARY KEY,
                    keyword VARCHAR(255) NOT NULL,
                    tax_id VARCHAR(50),
                    company_name VARCHAR(500),
                    address VARCHAR(1000),
                    legal_representative VARCHAR(255),
                    start_date VARCHAR(100),
                    status VARCHAR(255),
                    company_type VARCHAR(255),
                    email VARCHAR(255),
                    phone VARCHAR(50),
                    raw_data JSONB,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
                
                cursor.execute(create_table_query)
                conn.commit()
                logger.info("Database tables created successfully")
                
        except Exception as e:
            logger.error(f"Error creating tables: {e}")
            raise
    
    def save_company_info(self, keyword, tax_info, contact_info):
        """Lưu dữ liệu vào PostgreSQL"""
        try:
            # Chuẩn bị dữ liệu
            tax_data = tax_info or {}
            contact_data = contact_info or {}
            
            # Logic phone priority (giữ nguyên)
            final_phone = None
            phone_source = None
            
            if tax_data.get('phone'):
                final_phone = tax_data.get('phone')
                phone_source = "masothue.com"
            elif contact_data.get('phone'):
                final_phone = contact_data.get('phone')
                phone_source = "pdf"
            
            # Sử dụng Supabase client
            try:
                # Kiểm tra record tồn tại
                existing = self.supabase.table('company_info').select('id').eq('keyword', keyword).eq('tax_id', tax_data.get('taxID')).execute()
                
                data = {
                    'keyword': keyword,
                    'tax_id': tax_data.get('taxID'),
                    'company_name': tax_data.get('companyName'),
                    'address': tax_data.get('address'),
                    'legal_representative': tax_data.get('legalRepresentative'),
                    'start_date': tax_data.get('startDate'),
                    'status': tax_data.get('status'),
                    'company_type': tax_data.get('companyType'),
                    'email': contact_data.get('email'),
                    'phone': final_phone,
                    'raw_data': {
                        'tax_info': tax_data,
                        'contact_info': contact_data,
                        'phone_source': phone_source,
                        'final_phone': final_phone
                    }
                }
                
                if existing.data:
                    # Update
                    data['updated_at'] = 'now()'
                    result = self.supabase.table('company_info').update(data).eq('id', existing.data[0]['id']).execute()
                    logger.info(f"Updated existing record for keyword: {keyword}")
                else:
                    # Insert
                    result = self.supabase.table('company_info').insert(data).execute()
                    logger.info(f"Inserted new record for keyword: {keyword}")
                
                return True
                
            except Exception as supabase_error:
                logger.error(f"Supabase operation failed: {supabase_error}")
                # Fallback to direct PostgreSQL
                return self._save_with_postgres(keyword, tax_data, contact_data, final_phone, phone_source)
                
        except Exception as e:
            logger.error(f"Error saving company info: {e}")
            return False
    
    def _save_with_postgres(self, keyword, tax_data, contact_data, final_phone, phone_source):
        """Fallback PostgreSQL save method"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                # Check existing
                cursor.execute(
                    "SELECT id FROM company_info WHERE keyword = %s AND tax_id = %s",
                    (keyword, tax_data.get('taxID'))
                )
                existing = cursor.fetchone()
                
                if existing:
                    # Update
                    cursor.execute("""
                        UPDATE company_info 
                        SET company_name = %s, address = %s, legal_representative = %s,
                            start_date = %s, status = %s, company_type = %s,
                            email = %s, phone = %s, raw_data = %s, updated_at = CURRENT_TIMESTAMP
                        WHERE id = %s
                    """, (
                        tax_data.get('companyName'),
                        tax_data.get('address'),
                        tax_data.get('legalRepresentative'),
                        tax_data.get('startDate'),
                        tax_data.get('status'),
                        tax_data.get('companyType'),
                        contact_data.get('email'),
                        final_phone,
                        json.dumps({
                            'tax_info': tax_data,
                            'contact_info': contact_data,
                            'phone_source': phone_source,
                            'final_phone': final_phone
                        }),
                        existing['id']
                    ))
                else:
                    # Insert
                    cursor.execute("""
                        INSERT INTO company_info 
                        (keyword, tax_id, company_name, address, legal_representative,
                        start_date, status, company_type, email, phone, raw_data)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        keyword,
                        tax_data.get('taxID'),
                        tax_data.get('companyName'),
                        tax_data.get('address'),
                        tax_data.get('legalRepresentative'),
                        tax_data.get('startDate'),
                        tax_data.get('status'),
                        tax_data.get('companyType'),
                        contact_data.get('email'),
                        final_phone,
                        json.dumps({
                            'tax_info': tax_data,
                            'contact_info': contact_data,
                            'phone_source': phone_source,
                            'final_phone': final_phone
                        })
                    ))
                
                conn.commit()
                return True
                
        except Exception as e:
            logger.error(f"PostgreSQL save failed: {e}")
            return False
    
    def get_company_info(self, keyword=None, tax_id=None):
        """Lấy thông tin công ty"""
        try:
            query = self.supabase.table('company_info').select('*')
            
            if keyword:
                query = query.eq('keyword', keyword)
            elif tax_id:
                query = query.eq('tax_id', tax_id)
            
            result = query.order('created_at', desc=True).execute()
            
            return result.data if result.data else []
            
        except Exception as e:
            logger.error(f"Error getting company info: {e}")
            return []
        
def extract_text_pdfminer(pdf_path):
    """Trích xuất text từ PDF bằng pdfminer"""
    try:
        laparams = LAParams(
            boxes_flow=0.5,
            word_margin=0.1,
            char_margin=2.0,
            line_margin=0.5,
            detect_vertical=True
        )
        
        text = extract_text(pdf_path, laparams=laparams)
        return text
    except Exception as e:
        logger.error(f"Lỗi khi trích xuất với pdfminer: {e}")
        return None

def clean_text(text):
    """Làm sạch text sau khi trích xuất"""
    if not text:
        return text
    
    # Loại bỏ các ký tự không mong muốn
    text = text.replace('\x00', '')
    text = text.replace('\ufeff', '')
    
    # Xử lý các ký tự đặc biệt trong tiếng Việt
    replacements = {
        '(cid:264)': 'Đ',
        '(cid:255)': 'đ',
        '(cid:105)': 'á',
        '(cid:106)': 'à',
        '(cid:107)': 'â',
        '(cid:109)': 'ã',
        '(cid:116)': 'í',
        '(cid:117)': 'ì',
        '(cid:121)': 'ó',
        '(cid:122)': 'ò',
        '(cid:123)': 'ô'
    }
    
    for old, new in replacements.items():
        text = text.replace(old, new)
    
    # Loại bỏ dòng trống thừa và khoảng trắng thừa
    text = re.sub(r'\n\s*\n', '\n\n', text)
    text = re.sub(r' +', ' ', text)
    
    return text.strip()



def extract_contact_info(text):
    """Trích xuất chỉ email và điện thoại từ text"""
    if not text:
        return {}
    
    info = {}
    
    # Làm sạch text trước khi xử lý
    text = re.sub(r'\s+', ' ', text)
    
    # Trích xuất mã số doanh nghiệp để tránh nhầm lẫn với số điện thoại
    tax_code = ""
    tax_patterns = [
        r'Mã số doanh nghiệp:\s*(\d{10})',
        r'Mã số doanh nghiệp\s*(\d{10})',
        r'(?:^|\s)(\d{10})(?=\s|$)'
    ]
    
    for pattern in tax_patterns:
        match = re.search(pattern, text, re.MULTILINE)
        if match:
            tax_code = match.group(1)
            break
    
    # 1. Trích xuất điện thoại - Lấy số điện thoại xuất hiện đầu tiên
    # Tạo pattern tổng hợp để tìm tất cả số điện thoại có thể
    phone_pattern = r'(?:Điện thoại:\s*|Tel:\s*|Phone:\s*)?(\d{2,4}[\.\-\s]?\d{3,4}[\.\-\s]?\d{3,4}[\.\-\s]?\d{0,4}|\d{9,11})'
    
    # Tìm tất cả match với vị trí xuất hiện
    phone_matches = []
    for match in re.finditer(phone_pattern, text, re.MULTILINE | re.IGNORECASE):
        phone_candidate = match.group(1)
        position = match.start()
        phone_matches.append((position, phone_candidate))
    
    # Sắp xếp theo vị trí xuất hiện
    phone_matches.sort(key=lambda x: x[0])
    
    # Kiểm tra từng số theo thứ tự xuất hiện
    for position, phone in phone_matches:
        clean_phone = re.sub(r'[\.\-\s]', '', phone)
        
        # Tránh nhầm lẫn với mã số thuế
        if clean_phone == tax_code:
            continue

        if tax_code and (clean_phone.startswith(tax_code) or tax_code.startswith(clean_phone)):
            continue

        # Kiểm tra độ dài và prefix hợp lệ
        if len(clean_phone) >= 9 and len(clean_phone) <= 11:
            # Kiểm tra các đầu số hợp lệ của Việt Nam
            valid_prefixes = [
                '01', '02', '03', '05', '07', '08', '09',  # Di động
                '024', '028', '0236', '0256', '0274', '0204',  # Cố định
                '84'  # Mã quốc gia
            ]
            
            # Kiểm tra xem số có bắt đầu bằng prefix hợp lệ không
            is_valid = False
            for prefix in valid_prefixes:
                if clean_phone.startswith(prefix):
                    is_valid = True
                    break
            
            # Hoặc kiểm tra nếu là số cố định bắt đầu bằng 0 và có 10-11 chữ số
            if not is_valid and clean_phone.startswith('0') and len(clean_phone) in [10, 11]:
                is_valid = True
            
            if is_valid:
                info['phone'] = phone
                break
    
    # 2. Trích xuất email
    email_patterns = [
        r'Email:\s*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})',
        r'Email\s*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})',
        r'([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})'
    ]
    
    for pattern in email_patterns:
        matches = re.findall(pattern, text)
        if matches:
            for email in matches:
                if '@' in email and '.' in email:
                    info['email'] = email
                    break
            if 'email' in info:
                break
    
    return info




def extract_pdf_contact_info(pdf_path):
    """Trích xuất thông tin liên hệ từ PDF"""
    try:
        logger.info(f"Extracting contact info from PDF: {pdf_path}")
        
        # Trích xuất text
        raw_text = extract_text_pdfminer(pdf_path)
        
        if not raw_text or not raw_text.strip():
            logger.error("No text extracted from PDF")
            return None
        
        # Làm sạch text
        clean_text_result = clean_text(raw_text)
        
        # Trích xuất thông tin liên hệ
        contact_info = extract_contact_info(clean_text_result)
        
        logger.info(f"Extracted contact info: {contact_info}")
        
        return contact_info
        
    except Exception as e:
        logger.error(f"Error extracting PDF contact info: {e}")
        return None

solver = CaptchaSolver(CAPTCHA_API_KEY)
db_manager = DatabaseManager(SUPABASE_CONFIG)

async def inject_captcha_response(page, captcha_code):
    """Inject captcha response safely"""
    try:
        if page.is_closed():
            raise Exception("Page is closed")
            
        await page.evaluate(f'''
            let responseEl = document.getElementById("g-recaptcha-response");
            if (!responseEl) {{
                responseEl = document.createElement("textarea");
                responseEl.id = "g-recaptcha-response";
                responseEl.name = "g-recaptcha-response";
                responseEl.style.display = "none";
                document.body.appendChild(responseEl);
            }}
            responseEl.value = "{captcha_code}";
            
            document.querySelectorAll('.g-recaptcha-response').forEach(el => {{
                el.value = "{captcha_code}";
            }});
            
            console.log("Captcha response injected");
        ''')
        
        return True
    except Exception as e:
        logger.error(f"Injection failed: {e}")
        return False

async def crawl_and_download_pdf(mst: str):
    browser = None
    try:
        async with async_playwright() as p:
            # Trong function crawl_and_download_pdf và get_tax_info_internal
            browser = await p.chromium.launch(
                headless=True,  # Bắt buộc phải là True trên server
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox', 
                    '--disable-dev-shm-usage',
                    '--disable-extensions',
                    '--disable-plugins',
                    '--disable-blink-features=AutomationControlled',
                    '--disable-background-timer-throttling',
                    '--disable-backgrounding-occluded-windows',
                    '--disable-renderer-backgrounding',
                    '--disable-features=TranslateUI',
                    '--disable-ipc-flooding-protection',
                    '--single-process'  # Quan trọng cho môi trường container
                ]
            )
            
            context = await browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            )
            
            page = await context.new_page()
            
            # Navigate to page
            logger.info(f"Navigating to target URL for MST: {mst}")
            await page.goto(TARGET_URL, timeout=60000)
            await page.wait_for_load_state('networkidle')
            
            # Fill form
            logger.info("Filling form...")
            await page.select_option('#ctl00_C_ANNOUNCEMENT_TYPE_IDFilterFld', 'AMEND')
            await page.wait_for_timeout(2000)
            await page.fill('#ctl00_C_ENT_GDT_CODEFld', mst)
            
            # Solve captcha
            logger.info("Solving captcha...")
            captcha_code = await asyncio.to_thread(solver.solve_recaptcha, SITE_KEY, TARGET_URL)
            logger.info("Captcha solved successfully")
            
            # Inject captcha response
            success = await inject_captcha_response(page, captcha_code)
            if not success:
                raise Exception("Failed to inject captcha response")
            
            await page.wait_for_timeout(2000)
            
            # Submit form
            await page.click('#ctl00_C_BtnFilter')
            logger.info("Clicked search button")
            
            # Wait for page to reload and show results
            await page.wait_for_load_state('networkidle', timeout=30000)
            
            # Wait for results table
            try:
                await page.wait_for_selector('#ctl00_C_CtlList', timeout=30000)
                logger.info("Results table found")
            except:
                no_results = await page.query_selector('text=Không tìm thấy dữ liệu')
                if no_results:
                    logger.info("No results found for this MST")
                    return None
                raise Exception("Results table not found")
            
            # Find PDF button
            pdf_button = await page.query_selector('input[id^="ctl00_C_CtlList_"][id$="_LnkGetPDFActive"]')
            if not pdf_button:
                logger.info("No PDF button found - no results available")
                return None
            
            # Download PDF
            file_name = f"{mst}_{uuid.uuid4().hex[:8]}.pdf"
            download_path = os.path.join(os.getcwd(), file_name)
            
            # Setup download handler
            async with page.expect_download() as download_info:
                await pdf_button.click()
                logger.info("Clicked PDF download button")
            
            download = await download_info.value
            await download.save_as(download_path)
            logger.info(f"PDF downloaded: {file_name}")
            
            return file_name
            
    except Exception as e:
        logger.error(f"Crawl failed: {e}")
        raise
    finally:
        if browser:
            try:
                await browser.close()
            except:
                pass

    
async def get_tax_info_internal(keyword: str, max_retries: int = 3):
    """Fixed tax info function with correct Playwright syntax"""
    browser = None
    
    for attempt in range(max_retries):
        try:
            logger.info(f"Tax info attempt {attempt + 1}/{max_retries} for keyword: {keyword}")
            
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,  # Bắt buộc phải là True trên server
                    args=[
                        '--no-sandbox',
                        '--disable-setuid-sandbox', 
                        '--disable-dev-shm-usage',
                        '--disable-extensions',
                        '--disable-plugins',
                        '--disable-blink-features=AutomationControlled',
                        '--disable-background-timer-throttling',
                        '--disable-backgrounding-occluded-windows',
                        '--disable-renderer-backgrounding',
                        '--disable-features=TranslateUI',
                        '--disable-ipc-flooding-protection',
                        '--single-process'
                    ]
                )
                
                # Tạo context KHÔNG có timeout parameter
                context = await browser.new_context(
                    viewport={'width': 1280, 'height': 800},
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114 Safari/537.36'
                )
                
                # Set timeout cho context sau khi tạo
                context.set_default_timeout(60000)  # 1 phút
                
                page = await context.new_page()
                
                # Set timeout cho page
                page.set_default_timeout(60000)
                
                # Chặn tài nguyên không cần thiết
                # Chặn tài nguyên không cần thiết với exception handling
                async def handle_route(route):
                    try:
                        if route.request.resource_type in ["image", "font", "media"]:
                            await route.abort()
                        else:
                            await route.continue_()
                    except Exception as e:
                        logger.warning(f"Route handling error: {e}")
                        try:
                            await route.continue_()
                        except:
                            pass

                await page.route("**/*", handle_route)
                
                logger.info(f"Navigating to masothue.com for keyword: {keyword}")
                
                # Truy cập trang web
                await page.goto('https://masothue.com', timeout=60000)
                await page.wait_for_load_state('domcontentloaded')
                
                # Đợi input search xuất hiện
                await page.wait_for_selector('input[name="q"]', timeout=60000)
                
                # Nhập keyword và tìm kiếm
                await page.fill('input[name="q"]', keyword)
                
                # Click nút tìm kiếm và đợi navigation
                await page.click('.btn-search-submit')
                await page.wait_for_load_state('domcontentloaded')
                
                # Đợi bảng kết quả tải xong
                await page.wait_for_selector('table.table-taxinfo tbody', timeout=60000)
                
                # Trích xuất dữ liệu
                result = await page.evaluate("""
                () => {
                    const result = {};

                // Lấy tên công ty từ header
                const companyNameHeader = document.querySelector('table.table-taxinfo thead th[itemprop="name"] .copy');
                if (companyNameHeader) {
                    result.companyName = companyNameHeader.getAttribute('title') || companyNameHeader.innerText.trim();
                }

                // Lấy các thông tin từ tbody
                const rows = Array.from(document.querySelectorAll('table.table-taxinfo tbody tr'));

                rows.forEach(row => {
                    const label = row.querySelector('td:first-child')?.innerText.trim();
                    const valueCell = row.querySelector('td:nth-child(2)');
                    if (!label || !valueCell) return;
                    
                    let value = valueCell.innerText.trim();
                    
                    // Xử lý trường hợp người đại diện - lấy tên từ thẻ a hoặc span
                    if (label.includes('Người đại diện')) {
                        const nameElement = valueCell.querySelector('[itemprop="name"]');
                        if (nameElement) {
                            value = nameElement.innerText.trim();
                        }
                    }
                    
                    // Xử lý trường hợp điện thoại - ưu tiên lấy từ span.copy
                    if (label.includes('Điện thoại')) {
                        // Thử lấy từ span có class="copy" trước
                        const phoneElement = valueCell.querySelector('span.copy');
                        if (phoneElement) {
                            const phoneNumber = phoneElement.getAttribute('title') || phoneElement.innerText.trim();
                            // Chỉ lấy số điện thoại nếu không bị ẩn
                            if (phoneNumber && !phoneNumber.includes('Bị ẩn') && !phoneNumber.includes('*') && phoneNumber.length > 5) {
                                result.phone = phoneNumber;
                                console.log('Phone found from masothue.com:', phoneNumber);
                            }
                        } else {
                            // Fallback: lấy từ text content nếu không có span.copy
                            const phoneText = valueCell.innerText.trim();
                            if (phoneText && !phoneText.includes('Bị ẩn') && !phoneText.includes('*') && phoneText.length > 5) {
                                // Regex để extract phone number
                                const phoneMatch = phoneText.match(/(\d{2,4}[\.\-\s]?\d{3,4}[\.\-\s]?\d{3,4}[\.\-\s]?\d{0,4}|\d{9,11})/);
                                if (phoneMatch) {
                                    result.phone = phoneMatch[1];
                                    console.log('Phone extracted from text:', phoneMatch[1]);
                                }
                            }
                        }
                    }
                    
                    // Mapping các trường khác
                    if (label.includes('Mã số thuế')) {
                        result.taxID = value;
                    } else if (label.includes('Địa chỉ')) {
                        result.address = value;
                    } else if (label.includes('Người đại diện')) {
                        result.legalRepresentative = value;
                    } else if (label.includes('Ngày hoạt động')) {
                        result.startDate = value;
                    } else if (label.includes('Tình trạng')) {
                        result.status = value;
                    } else if (label.includes('Loại hình DN')) {
                        result.companyType = value;
                    }
                });

                    return result;
                    
                }
                """)

                
                logger.info(f"Successfully scraped tax info for keyword: {keyword}")
                
                return {
                    "keyword": keyword,
                    "data": result,
                    "status": "success"
                }
                
        except Exception as e:
            logger.error(f"Tax info attempt {attempt + 1} failed: {e}")
            if attempt == max_retries - 1:
                raise Exception(f"Failed to fetch tax info after {max_retries} attempts: {str(e)}")
            
            # Wait before retry
            wait_time = 2 ** attempt
            logger.info(f"Waiting {wait_time} seconds before retry...")
            await asyncio.sleep(wait_time)
                
        finally:
            if browser:
                try:
                    await browser.close()
                except:
                    pass

async def get_contact_info_internal(mst: str, max_retries: int = 3):
    """Enhanced contact info function with retry logic"""
    for attempt in range(max_retries):
        try:
            logger.info(f"Contact info attempt {attempt + 1}/{max_retries} for MST: {mst}")
            
            # Check balance
            balance = await asyncio.to_thread(solver.get_balance)
            if balance < 0.001:
                logger.warning("Insufficient balance for captcha solving")
                return None
            
            # Crawl and download with retry
            pdf_path = await crawl_and_download_pdf(mst, max_retries=2)
            
            if not pdf_path or not os.path.exists(pdf_path):
                logger.info("No PDF found for MST")
                return None
            
            # Extract contact information from PDF
            contact_info = extract_pdf_contact_info(pdf_path)
            
            # Clean up PDF file
            try:
                os.remove(pdf_path)
                logger.info(f"Cleaned up PDF file: {pdf_path}")
            except:
                pass
            
            if not contact_info:
                logger.info("No contact information found in PDF")
                return None
            
            return {
                "mst": mst,
                "email": contact_info.get('email'),
                "phone": contact_info.get('phone')
            }
            
        except Exception as e:
            logger.error(f"Contact info attempt {attempt + 1} failed: {e}")
            if attempt == max_retries - 1:
                logger.error(f"All {max_retries} attempts failed for MST: {mst}")
                return None
            
            # Wait before retry
            wait_time = 2 ** attempt
            logger.info(f"Waiting {wait_time} seconds before retry...")
            await asyncio.sleep(wait_time)

port = int(os.environ.get("PORT", 8000))


@app.get("/combined-info")
async def get_combined_info_api(keyword: str = Query(..., min_length=1, description="Keyword to search for tax information")):
    """
    API endpoint để lấy thông tin thuế trước, sau đó lấy thông tin liên hệ và ghép lại
    """
    try:
        # Step 1: Lấy thông tin thuế trước
        logger.info(f"Step 1: Getting tax info for keyword: {keyword}")
        
        tax_info = await get_tax_info_internal(keyword)
        
        if not tax_info or not tax_info.get('data'):
            return JSONResponse({
                "error": "No tax information found for the given keyword",
                "keyword": keyword
            }, status_code=404)
        
        # Lấy MST từ tax_info
        mst = tax_info['data'].get('taxID')
        if not mst:
            return JSONResponse({
                "error": "Tax ID not found in tax information",
                "keyword": keyword,
                "tax_info": tax_info
            }, status_code=404)
        
        logger.info(f"Found MST: {mst}")
        
        # Step 2: Lấy thông tin liên hệ
        logger.info(f"Step 2: Getting contact info for MST: {mst}")
        
        contact_info = await get_contact_info_internal(mst)
        
        # Step 3: Ghép kết quả
        combined_result = {
            "keyword": keyword,
            "tax_info": tax_info['data'],
            "contact_info": contact_info if contact_info else {
                "email": None,
                "phone": None,
                "note": "No contact information found or PDF not available"
            },
            "status": "success"
        }
        
        logger.info(f"Successfully combined information for keyword: {keyword}")
        
        return JSONResponse(combined_result)
        
    except Exception as e:
        logger.error(f"Combined API error: {e}")
        return JSONResponse({
            "error": f"Failed to get combined information: {str(e)}",
            "keyword": keyword
        }, status_code=500)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port)
