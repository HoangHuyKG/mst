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

# Cấu hình logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI()

# Configuration
CAPTCHA_API_KEY = 'ac625ff477cc0aabf9ce74f3d47472fc'
TARGET_URL = "https://dangkyquamang.dkkd.gov.vn/egazette/Forms/Egazette/ANNOUNCEMENTSListingInsUpd.aspx"
SITE_KEY = "6LewYU4UAAAAAD9dQ51Cj_A_1uHLOXw9wJIxi9x0"

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

import re

import re

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

@app.get("/get-contact-info")
async def get_contact_info_api(mst: str = Query(..., min_length=10, max_length=14)):
    """API endpoint để lấy thông tin liên hệ (email và điện thoại)"""
    try:
        # Check balance
        balance = await asyncio.to_thread(solver.get_balance)
        if balance < 0.001:
            return JSONResponse({"error": "Insufficient balance"}, status_code=400)
        
        # Crawl and download
        pdf_path = await crawl_and_download_pdf(mst)
        
        if not pdf_path or not os.path.exists(pdf_path):
            return JSONResponse({"error": "No results found"}, status_code=404)
        
        # Extract contact information from PDF
        contact_info = extract_pdf_contact_info(pdf_path)
        
        if not contact_info:
            return JSONResponse({"error": "No contact information found in PDF"}, status_code=404)
        
        # Clean up PDF file
        try:
            os.remove(pdf_path)
            logger.info(f"Cleaned up PDF file: {pdf_path}")
        except:
            pass
        
        return JSONResponse({
            "mst": mst,
            "email": contact_info.get('email'),
            "phone": contact_info.get('phone'),
            "status": "success"
        })
        
    except Exception as e:
        logger.error(f"API error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

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

async def get_tax_info_internal(keyword: str):
    """
    Internal function để lấy thông tin thuế (tương tự như API /tax-info)
    """
    browser = None
    try:
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
        '--single-process'  # Quan trọng cho môi trường container
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
            
            return {
                "keyword": keyword,
                "data": result,
                "status": "success"
            }
            
    except Exception as e:
        logger.error(f"Tax info scraping failed: {e}")
        raise Exception(f"Failed to fetch tax info: {str(e)}")
        
    finally:
        if browser:
            try:
                await browser.close()
            except:
                pass

async def get_contact_info_internal(mst: str):
    """
    Internal function để lấy thông tin liên hệ (tương tự như API /get-contact-info)
    """
    try:
        # Check balance
        balance = await asyncio.to_thread(solver.get_balance)
        if balance < 0.001:
            logger.warning("Insufficient balance for captcha solving")
            return None
        
        # Crawl and download
        pdf_path = await crawl_and_download_pdf(mst)
        
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
        logger.error(f"Contact info extraction failed: {e}")
        return None

port = int(os.environ.get("PORT", 8000))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port)
