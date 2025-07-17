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

async def crawl_and_download_pdf(mst: str, max_retries: int = 3):
    """Enhanced crawl function with better error handling and timeout management"""
    browser = None
    
    # Danh sách các loại đăng ký để thử theo thứ tự
    registration_types = [
        ('NEW', 'Đăng ký mới'),
        ('AMEND', 'Đăng ký thay đổi')
    ]
    
    for attempt in range(max_retries):
        try:
            logger.info(f"Attempt {attempt + 1}/{max_retries} for MST: {mst}")
            
            async with async_playwright() as p:
                # Cấu hình browser với timeout ngắn hơn
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        '--no-sandbox',
                        '--disable-setuid-sandbox',
                        '--disable-dev-shm-usage',
                        '--disable-blink-features=AutomationControlled',
                        '--disable-web-security',
                        '--disable-features=VizDisplayCompositor',
                        '--disable-background-timer-throttling',
                        '--disable-backgrounding-occluded-windows',
                        '--disable-renderer-backgrounding',
                        '--disable-background-networking',
                        '--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                    ],
                    ignore_default_args=['--disable-extensions'],
                    slow_mo=100  # Thêm delay giữa các thao tác
                )
                
                context = await browser.new_context(
                    viewport={'width': 1366, 'height': 768},
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    extra_http_headers={
                        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                        'Accept-Language': 'vi-VN,vi;q=0.9,en;q=0.8',
                        'Accept-Encoding': 'gzip, deflate, br',
                        'Connection': 'keep-alive',
                        'Upgrade-Insecure-Requests': '1',
                        'Cache-Control': 'no-cache'
                    },
                    ignore_https_errors=True,
                    java_script_enabled=True,
                    bypass_csp=True
                )
                
                # Giảm timeout xuống 90 giây thay vì 180 giây
                context.set_default_timeout(90000)
                
                # Chặn nhiều tài nguyên hơn để tăng tốc
                await context.route("**/*", lambda route: (
                    route.abort() if route.request.resource_type in [
                        "image", "media", "font", "stylesheet", "manifest", "other"
                    ] else route.continue_()
                ))
                
                page = await context.new_page()
                page.set_default_timeout(90000)
                
                # Error handlers được cải thiện
                async def handle_page_error(error):
                    logger.error(f"Page error: {error}")

                async def handle_console(msg):
                    if msg.type == "error" and any(err in msg.text for err in [
                        "net::ERR_FAILED", "net::ERR_ABORTED", "net::ERR_BLOCKED_BY_CLIENT",
                        "net::ERR_INTERNET_DISCONNECTED", "net::ERR_NETWORK_CHANGED"
                    ]):
                        return
                    if msg.type == "error":
                        logger.info(f"Console: {msg.text}")

                page.on("pageerror", handle_page_error)
                page.on("console", handle_console)
                
                # Thử từng loại đăng ký
                for reg_type, reg_name in registration_types:
                    logger.info(f"Trying registration type: {reg_name} ({reg_type})")
                    
                    try:
                        # Step 1: Navigation với logic cải tiến
                        logger.info(f"Navigating to target URL for MST: {mst}")
                        
                        navigation_success = False
                        for nav_attempt in range(3):
                            try:
                                logger.info(f"Navigation attempt {nav_attempt + 1}/3")
                                
                                # Thử với wait_until khác nhau
                                wait_strategies = ['domcontentloaded', 'load', 'networkidle']
                                wait_until = wait_strategies[nav_attempt % len(wait_strategies)]
                                
                                # Giảm timeout navigation xuống 60 giây
                                response = await page.goto(
                                    TARGET_URL, 
                                    timeout=60000, 
                                    wait_until=wait_until
                                )
                                
                                if response and response.status >= 400:
                                    logger.error(f"HTTP error: {response.status}")
                                    raise Exception(f"HTTP {response.status} error")
                                
                                logger.info(f"Successfully navigated to target URL. Status: {response.status if response else 'Unknown'}")
                                
                                # Kiểm tra xem trang đã load xong chưa
                                try:
                                    await page.wait_for_load_state('domcontentloaded', timeout=30000)
                                    navigation_success = True
                                    break
                                except:
                                    logger.warning("DOMContentLoaded timeout, but continuing...")
                                    navigation_success = True
                                    break
                                
                            except Exception as nav_error:
                                logger.warning(f"Navigation attempt {nav_attempt + 1} failed: {nav_error}")
                                if nav_attempt == 2:
                                    logger.error("All navigation attempts failed")
                                    raise nav_error
                                
                                # Tăng dần thời gian chờ giữa các lần thử
                                wait_time = (nav_attempt + 1) * 2
                                logger.info(f"Waiting {wait_time} seconds before retry...")
                                await asyncio.sleep(wait_time)
                        
                        if not navigation_success:
                            raise Exception("Navigation failed after all attempts")
                        
                        # Đợi thêm một chút để trang ổn định
                        await page.wait_for_timeout(3000)
                        
                        # Step 2: Wait for form elements với timeout ngắn hơn
                        logger.info("Waiting for form elements...")
                        
                        selectors_to_try = [
                            '#ctl00_C_ANNOUNCEMENT_TYPE_IDFilterFld',
                            'select[name*="ANNOUNCEMENT_TYPE"]',
                            'select[id*="ANNOUNCEMENT_TYPE"]'
                        ]
                        
                        form_found = False
                        for selector in selectors_to_try:
                            try:
                                await page.wait_for_selector(selector, timeout=20000)
                                logger.info(f"Found form element with selector: {selector}")
                                form_found = True
                                break
                            except Exception as e:
                                logger.warning(f"Selector {selector} not found: {e}")
                                continue
                        
                        if not form_found:
                            # Thử reload trang một lần nữa
                            logger.warning("Form not found, trying to reload page...")
                            await page.reload(timeout=60000, wait_until='domcontentloaded')
                            await page.wait_for_timeout(5000)
                            
                            # Thử lại tìm form
                            for selector in selectors_to_try:
                                try:
                                    await page.wait_for_selector(selector, timeout=15000)
                                    logger.info(f"Found form element after reload with selector: {selector}")
                                    form_found = True
                                    break
                                except:
                                    continue
                        
                        if not form_found:
                            page_content = await page.content()
                            logger.error("Form not found even after reload. Page content preview:")
                            logger.error(page_content[:1000] + "...")
                            raise Exception("Form elements not found on page")
                        
                        # Step 3: Fill form với retry logic
                        logger.info(f"Filling form with registration type: {reg_name}")
                        
                        for fill_attempt in range(3):
                            try:
                                # Đợi element có thể tương tác
                                await page.wait_for_selector('#ctl00_C_ANNOUNCEMENT_TYPE_IDFilterFld', state='attached', timeout=10000)
                                await page.select_option('#ctl00_C_ANNOUNCEMENT_TYPE_IDFilterFld', reg_type)
                                await page.wait_for_timeout(2000)
                                
                                await page.wait_for_selector('#ctl00_C_ENT_GDT_CODEFld', state='attached', timeout=10000)
                                await page.fill('#ctl00_C_ENT_GDT_CODEFld', mst)
                                await page.wait_for_timeout(1000)
                                
                                logger.info("Form filled successfully")
                                break
                                
                            except Exception as form_error:
                                logger.warning(f"Form filling attempt {fill_attempt + 1} failed: {form_error}")
                                if fill_attempt == 2:
                                    logger.error(f"Form filling failed after all attempts: {form_error}")
                                    raise form_error
                                await page.wait_for_timeout(2000)
                        
                        # Step 4: Solve captcha với retry
                        logger.info("Solving captcha...")
                        captcha_code = None
                        
                        for captcha_attempt in range(3):
                            try:
                                captcha_code = await asyncio.wait_for(
                                    asyncio.to_thread(solver.solve_recaptcha, SITE_KEY, TARGET_URL),
                                    timeout=120  # 2 phút timeout cho captcha
                                )
                                logger.info("Captcha solved successfully")
                                break
                            except asyncio.TimeoutError:
                                logger.warning(f"Captcha timeout on attempt {captcha_attempt + 1}")
                                if captcha_attempt == 2:
                                    raise Exception("Captcha solving timeout")
                            except Exception as captcha_error:
                                logger.warning(f"Captcha attempt {captcha_attempt + 1} failed: {captcha_error}")
                                if captcha_attempt == 2:
                                    raise captcha_error
                                await asyncio.sleep(3)
                        
                        if not captcha_code:
                            raise Exception("Failed to solve captcha")
                        
                        # Step 5: Inject captcha
                        success = await inject_captcha_response(page, captcha_code)
                        if not success:
                            raise Exception("Failed to inject captcha response")
                        
                        await page.wait_for_timeout(2000)
                        
                        # Step 6: Submit form
                        logger.info("Submitting form...")
                        
                        try:
                            await page.click('#ctl00_C_BtnFilter')
                            logger.info("Form submitted successfully")
                        except Exception as submit_error:
                            logger.error(f"Form submission failed: {submit_error}")
                            raise submit_error
                        
                        # Step 7: Wait for results với timeout ngắn hơn
                        logger.info("Waiting for results...")
                        
                        try:
                            await page.wait_for_load_state('domcontentloaded', timeout=60000)
                        except:
                            logger.warning("Load state timeout, but continuing...")
                        
                        # Check for results table
                        try:
                            await page.wait_for_selector('#ctl00_C_CtlList', timeout=30000)
                            logger.info("Results table found")
                        except Exception:
                            # Check for no results message
                            try:
                                no_results_selectors = [
                                    'text=Không tìm thấy dữ liệu',
                                    'text=No data found',
                                    '.no-results',
                                    '[id*="NoData"]'
                                ]
                                
                                for selector in no_results_selectors:
                                    try:
                                        await page.wait_for_selector(selector, timeout=5000)
                                        logger.info(f"No results found with selector: {selector} for {reg_name}")
                                        break
                                    except:
                                        continue
                                else:
                                    logger.error("Results table not found within timeout")
                                    
                                    # Debug info
                                    current_url = page.url
                                    page_title = await page.title()
                                    logger.error(f"Current URL: {current_url}")
                                    logger.error(f"Page title: {page_title}")
                                    
                                    # Screenshot cho debug
                                    try:
                                        screenshot_path = f"debug_{mst}_{reg_type}_{attempt}.png"
                                        await page.screenshot(path=screenshot_path)
                                        logger.info(f"Screenshot saved: {screenshot_path}")
                                    except:
                                        pass
                                    
                                    raise Exception("Results table not found")
                                
                                # Nếu không có kết quả, thử loại đăng ký tiếp theo
                                logger.info(f"No results found for {reg_name}, trying next registration type...")
                                continue
                            except:
                                pass
                        
                        # Step 8: Find and download PDF
                        logger.info("Looking for PDF download button...")
                        
                        pdf_selectors = [
                            'input[id*="LnkGetPDFActive"]',
                            'input[type="image"][src*="pdf"]',
                            'input[name*="LnkGetPDFActive"]',
                            'input[id^="ctl00_C_CtlList_"][id$="_LnkGetPDFActive"]',
                            'input[src*="pdf.png"]'
                        ]
                        
                        pdf_button = None
                        for selector in pdf_selectors:
                            try:
                                pdf_button = await page.query_selector(selector)
                                if pdf_button:
                                    logger.info(f"Found PDF button with selector: {selector}")
                                    break
                            except Exception as e:
                                logger.warning(f"PDF selector {selector} failed: {e}")
                                continue
                        
                        if not pdf_button:
                            logger.info(f"No PDF button found for {reg_name}")
                            if reg_type == registration_types[-1][0]:
                                logger.info("No PDF found in any registration type")
                                return None
                            else:
                                logger.info(f"Trying next registration type...")
                                continue
                        
                        # Step 9: Download PDF
                        logger.info(f"Downloading PDF for {reg_name}...")
                        
                        file_name = f"{mst}_{reg_type}_{uuid.uuid4().hex[:8]}.pdf"
                        download_path = os.path.join(os.getcwd(), file_name)
                        
                        try:
                            async with page.expect_download(timeout=90000) as download_info:
                                await pdf_button.click()
                                logger.info("Clicked PDF download button")
                            
                            download = await download_info.value
                            await download.save_as(download_path)
                            logger.info(f"PDF downloaded successfully: {file_name}")
                            
                            # Verify file
                            if os.path.exists(download_path) and os.path.getsize(download_path) > 0:
                                logger.info(f"PDF file verified: {os.path.getsize(download_path)} bytes")
                                logger.info(f"Successfully downloaded PDF from {reg_name}")
                                return file_name
                            else:
                                logger.error("Downloaded PDF file is empty or doesn't exist")
                                if reg_type != registration_types[-1][0]:
                                    logger.info("Empty PDF, trying next registration type...")
                                    continue
                                else:
                                    return None
                            
                        except Exception as download_error:
                            logger.error(f"Download failed for {reg_name}: {download_error}")
                            if reg_type != registration_types[-1][0]:
                                logger.info("Download failed, trying next registration type...")
                                continue
                            else:
                                raise Exception(f"PDF download failed for all registration types. Last error: {download_error}")
                    
                    except Exception as reg_error:
                        logger.error(f"Registration type {reg_name} failed: {reg_error}")
                        if reg_type != registration_types[-1][0]:
                            logger.info("Current registration type failed, trying next...")
                            continue
                        else:
                            raise reg_error
                
                # Nếu đã thử hết tất cả các loại đăng ký mà không thành công
                logger.error("All registration types failed")
                return None
                
        except Exception as e:
            logger.error(f"Attempt {attempt + 1} failed: {e}")
            if attempt == max_retries - 1:
                raise Exception(f"All {max_retries} attempts failed. Last error: {e}")
            
            # Tăng dần thời gian chờ giữa các lần thử
            wait_time = min(2 ** attempt, 30)  # Tối đa 30 giây
            logger.info(f"Waiting {wait_time} seconds before retry...")
            await asyncio.sleep(wait_time)
            
        finally:
            if browser:
                try:
                    await browser.close()
                except:
                    pass

# Thêm hàm helper để check network connectivity
async def check_network_connectivity():
    """Check if the target URL is accessible"""
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(TARGET_URL, timeout=aiohttp.ClientTimeout(total=10)) as response:
                return response.status == 200
    except:
        return False

# Cập nhật hàm get_contact_info_internal để check network trước
async def get_contact_info_internal(mst: str, max_retries: int = 3):
    """Enhanced contact info retrieval with network check"""
    
    # Check network connectivity trước
    if not await check_network_connectivity():
        logger.error("Network connectivity check failed")
        return None
    
    for attempt in range(max_retries):
        try:
            logger.info(f"Contact info attempt {attempt + 1}/{max_retries} for MST: {mst}")
            
            # Download PDF
            file_name = await crawl_and_download_pdf(mst, max_retries=2)
            
            if not file_name:
                logger.info(f"No PDF file downloaded for MST: {mst}")
                return None
            
            # Extract contact info
            contact_info = await extract_contact_info(file_name)
            
            # Cleanup
            try:
                os.remove(file_name)
                logger.info(f"Cleaned up file: {file_name}")
            except:
                pass
            
            return contact_info
            
        except Exception as e:
            logger.error(f"Contact info attempt {attempt + 1} failed: {e}")
            if attempt == max_retries - 1:
                logger.error(f"All contact info attempts failed for MST: {mst}")
                return None
            
            # Exponential backoff
            wait_time = min(2 ** attempt, 30)
            logger.info(f"Waiting {wait_time} seconds before retry...")
            await asyncio.sleep(wait_time)
    
    return None


@app.get("/tax-info")
async def get_tax_info_api(keyword: str = Query(..., min_length=1, description="Keyword to search for tax information")):
    """
    API endpoint để scrape thông tin thuế từ masothue.com
    """
    browser = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox', 
                    '--disable-setuid-sandbox',
                    '--disable-blink-features=AutomationControlled',
                    '--disable-images',
                    '--disable-extensions',
                    '--disable-plugins'
                ]
            )
            
            context = await browser.new_context(
                viewport={'width': 1280, 'height': 800},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114 Safari/537.36'
            )
            
            page = await context.new_page()
            
            # Chặn tài nguyên không cần thiết
            await page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "font", "media"] else route.continue_())
            
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
                        
                        // Mapping chỉ các trường cần thiết
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
            
            return JSONResponse({
                "keyword": keyword,
                "data": result,
                "status": "success"
            })
            
    except Exception as e:
        logger.error(f"Tax info scraping failed: {e}")
        return JSONResponse({"error": f"Failed to fetch tax info: {str(e)}"}, status_code=500)
        
    finally:
        if browser:
            try:
                await browser.close()
            except:
                pass

@app.get("/combined-info")
async def get_combined_info_api(keyword: str = Query(..., min_length=1, description="Keyword to search for tax information")):
    """
    Enhanced API endpoint với phone priority logic: masothue.com phone first, then PDF phone
    """
    try:
        # Step 1: Get tax info with retry
        logger.info(f"Step 1: Getting tax info for keyword: {keyword}")
        
        tax_info = await get_tax_info_internal(keyword, max_retries=3)
        
        if not tax_info or not tax_info.get('data'):
            return JSONResponse({
                "error": "No tax information found for the given keyword",
                "keyword": keyword
            }, status_code=404)
        
        # Get MST from tax_info
        mst = tax_info['data'].get('taxID')
        if not mst:
            return JSONResponse({
                "error": "Tax ID not found in tax information",
                "keyword": keyword,
                "tax_info": tax_info
            }, status_code=404)
        
        logger.info(f"Found MST: {mst}")
        
        # Step 2: Get contact info with retry
        logger.info(f"Step 2: Getting contact info for MST: {mst}")
        
        contact_info = await get_contact_info_internal(mst, max_retries=3)
        
        # Step 3: Combine results with phone priority logic
        tax_data = tax_info['data']
        pdf_contact = contact_info if contact_info else {}

        # Logic ưu tiên: phone từ masothue.com, nếu không có thì lấy từ PDF
        # Email chỉ lấy từ PDF
        final_phone = None
        phone_source = None

        if tax_data.get('phone'):
            # Có phone từ masothue.com
            final_phone = tax_data.get('phone')
            phone_source = "masothue.com"
            logger.info(f"Using phone from masothue.com: {final_phone}")
        elif pdf_contact.get('phone'):
            # Không có phone từ masothue.com, lấy từ PDF
            final_phone = pdf_contact.get('phone')
            phone_source = "pdf"
            logger.info(f"Using phone from PDF: {final_phone}")
        else:
            logger.info("No phone number found from any source")

        # Tạo final_contact_info với phone đã được xử lý
        final_contact_info = {
            "phone": final_phone,
            "email": pdf_contact.get('email'),
            "phone_source": phone_source,
            "email_source": "pdf" if pdf_contact.get('email') else None
        }

        combined_result = {
            "keyword": keyword,
            "tax_info": tax_data,
            "contact_info": final_contact_info,
            "status": "success"
        }
        
        # Step 4: Save to database với phone đã được xử lý
        try:
            # Tạo contact_info_for_db với phone đã được ưu tiên
            contact_info_for_db = {
                "phone": final_phone,
                "email": pdf_contact.get('email'),
                "phone_source": phone_source,
                "email_source": "pdf" if pdf_contact.get('email') else None
            }
            
            db_saved = db_manager.save_company_info(
                keyword=keyword,
                tax_info=tax_data,
                contact_info=contact_info_for_db
            )
            
            if db_saved:
                combined_result["database_status"] = "saved"
                logger.info(f"Successfully saved to database for keyword: {keyword}")
            else:
                combined_result["database_status"] = "failed"
                logger.warning(f"Failed to save to database for keyword: {keyword}")
                
        except Exception as db_error:
            logger.error(f"Database save error: {db_error}")
            combined_result["database_status"] = "error"
            combined_result["database_error"] = str(db_error)
        
        logger.info(f"Successfully combined information for keyword: {keyword}")
        
        return JSONResponse(combined_result)
        
    except Exception as e:
        logger.error(f"Combined API error: {e}")
        return JSONResponse({
            "error": f"Failed to get combined information: {str(e)}",
            "keyword": keyword
        }, status_code=500)
    
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

 
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port)
