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
load_dotenv()  # Thêm dòng này sau các import

# Cấu hình logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI()

# Configuration
CAPTCHA_API_KEY = os.environ.get('CAPTCHA_API_KEY', 'your_default_key')
TARGET_URL = "https://dangkyquamang.dkkd.gov.vn/egazette/Forms/Egazette/ANNOUNCEMENTSListingInsUpd.aspx"
SITE_KEY = "6LewYU4UAAAAAD9dQ51Cj_A_1uHLOXw9wJIxi9x0"

SQL_SERVER_CONFIG = {
    'server': '26.25.148.0',  # IP của Radmin VPN
    'database': os.environ.get('SQL_DATABASE', 'CompanyDB'),
    'username': os.environ.get('SQL_USERNAME', 'sa'),
    'password': os.environ.get('SQL_PASSWORD', '123'),
    'driver': '{ODBC Driver 17 for SQL Server}',
    'port': int(os.environ.get('SQL_PORT', '1433')),
    'timeout': 60,
    'login_timeout': 60,
    'encrypt': 'no',
    'trust_server_certificate': 'yes'
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
        self.max_retries = 3
        self.retry_delay = 5  # giây
            
    def _build_connection_string(self):
        """Xây dựng connection string với nhiều tùy chọn"""
        # Thử connection string đầy đủ
        connection_string = (
            f"DRIVER={self.config['driver']};"
            f"SERVER={self.config['server']},{self.config['port']};"
            f"DATABASE={self.config['database']};"
            f"UID={self.config['username']};"
            f"PWD={self.config['password']};"
            f"Encrypt=no;"
            f"TrustServerCertificate=yes;"
            f"Connection Timeout={self.config['timeout']};"
            f"Login Timeout={self.config.get('login_timeout', 60)};"
            f"MultipleActiveResultSets=True;"
            f"ApplicationIntent=ReadWrite;"
        )
        return connection_string
    
    def _test_connection(self):
        """Test kết nối với server"""
        import socket
        try:
            # Test TCP connection
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10)
            result = sock.connect_ex((self.config['server'], self.config['port']))
            sock.close()
            
            if result == 0:
                logger.info(f"TCP connection to {self.config['server']}:{self.config['port']} successful")
                return True
            else:
                logger.error(f"TCP connection failed to {self.config['server']}:{self.config['port']}")
                return False
        except Exception as e:
            logger.error(f"Connection test failed: {e}")
            return False
    
    def get_connection(self):
        """Tạo kết nối đến SQL Server với retry logic"""
        last_error = None
        
        # Test TCP connection trước
        if not self._test_connection():
            raise Exception(f"Cannot reach SQL Server at {self.config['server']}:{self.config['port']}")
        
        for attempt in range(self.max_retries):
            try:
                logger.info(f"Attempting database connection {attempt + 1}/{self.max_retries}")
                
                connection_string = self._build_connection_string()
                logger.info(f"Connection string: {connection_string.replace(self.config['password'], '***')}")
                
                connection = pyodbc.connect(
                    connection_string,
                    timeout=self.config['timeout'],
                    autocommit=False
                )
                
                # Test connection bằng cách thực hiện một query đơn giản
                cursor = connection.cursor()
                cursor.execute("SELECT 1")
                cursor.fetchone()
                cursor.close()
                
                logger.info("Database connection successful!")
                return connection
                
            except pyodbc.Error as e:
                last_error = e
                error_code = e.args[0] if e.args else "Unknown"
                error_message = e.args[1] if len(e.args) > 1 else str(e)
                
                logger.error(f"Database connection attempt {attempt + 1} failed:")
                logger.error(f"Error Code: {error_code}")
                logger.error(f"Error Message: {error_message}")
                
                # Nếu là lỗi timeout hoặc network, thử lại
                if error_code in ['HYT00', '08001', '08S01'] and attempt < self.max_retries - 1:
                    logger.info(f"Retrying in {self.retry_delay} seconds...")
                    time.sleep(self.retry_delay)
                    continue
                else:
                    break
                    
            except Exception as e:
                last_error = e
                logger.error(f"Unexpected error on attempt {attempt + 1}: {e}")
                if attempt < self.max_retries - 1:
                    logger.info(f"Retrying in {self.retry_delay} seconds...")
                    time.sleep(self.retry_delay)
                else:
                    break
        
        # Nếu tất cả attempts đều thất bại
        error_msg = f"Database connection failed after {self.max_retries} attempts. Last error: {last_error}"
        logger.error(error_msg)
        raise Exception(error_msg)
    
    def test_connection_variants(self):
        """Test nhiều variant của connection string"""
        variants = [
            # Variant 1: Cơ bản với TCP
            {
                'name': 'TCP Connection',
                'string': f"DRIVER={self.config['driver']};SERVER={self.config['server']},{self.config['port']};DATABASE={self.config['database']};UID={self.config['username']};PWD={self.config['password']};Encrypt=no;TrustServerCertificate=yes;Connection Timeout=30;"
            },
            # Variant 2: Không chỉ định port
            {
                'name': 'Default Port',
                'string': f"DRIVER={self.config['driver']};SERVER={self.config['server']};DATABASE={self.config['database']};UID={self.config['username']};PWD={self.config['password']};Encrypt=no;TrustServerCertificate=yes;Connection Timeout=30;"
            },
            # Variant 3: Sử dụng IP với instance
            {
                'name': 'IP with Instance',
                'string': f"DRIVER={self.config['driver']};SERVER={self.config['server']}\\SQLEXPRESS;DATABASE={self.config['database']};UID={self.config['username']};PWD={self.config['password']};Encrypt=no;TrustServerCertificate=yes;Connection Timeout=30;"
            },
            # Variant 4: Trusted connection (nếu có thể)
            {
                'name': 'Windows Authentication',
                'string': f"DRIVER={self.config['driver']};SERVER={self.config['server']},{self.config['port']};DATABASE={self.config['database']};Trusted_Connection=yes;Encrypt=no;TrustServerCertificate=yes;Connection Timeout=30;"
            }
        ]
        
        for variant in variants:
            try:
                logger.info(f"Testing {variant['name']}...")
                connection = pyodbc.connect(variant['string'], timeout=30)
                cursor = connection.cursor()
                cursor.execute("SELECT 1")
                cursor.fetchone()
                cursor.close()
                connection.close()
                logger.info(f"✓ {variant['name']} successful!")
                return variant['string']
            except Exception as e:
                logger.error(f"✗ {variant['name']} failed: {e}")
                continue
        
        return None
    
    def create_tables(self):
        """Tạo bảng lưu trữ dữ liệu nếu chưa tồn tại"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                # Tạo bảng company_info
                create_table_query = """
                IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='company_info' AND xtype='U')
                CREATE TABLE company_info (
                    id INT IDENTITY(1,1) PRIMARY KEY,
                    keyword NVARCHAR(255) NOT NULL,
                    tax_id NVARCHAR(50),
                    company_name NVARCHAR(500),
                    address NVARCHAR(1000),
                    legal_representative NVARCHAR(255),
                    start_date NVARCHAR(100),
                    status NVARCHAR(255),
                    company_type NVARCHAR(255),
                    email NVARCHAR(255),
                    phone NVARCHAR(50),
                    raw_data NVARCHAR(MAX),
                    created_at DATETIME DEFAULT GETDATE(),
                    updated_at DATETIME DEFAULT GETDATE()
                )
                """
                
                cursor.execute(create_table_query)
                conn.commit()
                logger.info("Database tables created successfully")
                
        except Exception as e:
            logger.error(f"Error creating tables: {e}")
            raise
    
    def save_company_info(self, keyword, tax_info, contact_info):
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                # Chuẩn bị dữ liệu
                tax_data = tax_info or {}
                contact_data = contact_info or {}
                
                # Xử lý logic ưu tiên phone
                final_phone = None
                phone_source = None
                
                if tax_data.get('phone'):
                    # Ưu tiên phone từ masothue.com
                    final_phone = tax_data.get('phone')
                    phone_source = "masothue.com"
                elif contact_data.get('phone'):
                    # Fallback sang phone từ PDF
                    final_phone = contact_data.get('phone')
                    phone_source = "pdf"
                
                # Kiểm tra xem record đã tồn tại chưa
                check_query = "SELECT id FROM company_info WHERE keyword = ? AND tax_id = ?"
                cursor.execute(check_query, (keyword, tax_data.get('taxID')))
                existing_record = cursor.fetchone()
                
                if existing_record:
                    # Cập nhật record hiện tại
                    update_query = """
                    UPDATE company_info 
                    SET company_name = ?, address = ?, legal_representative = ?, 
                        start_date = ?, status = ?, company_type = ?, 
                        email = ?, phone = ?, raw_data = ?, updated_at = GETDATE()
                    WHERE id = ?
                    """
                    
                    cursor.execute(update_query, (
                        tax_data.get('companyName'),
                        tax_data.get('address'),
                        tax_data.get('legalRepresentative'),
                        tax_data.get('startDate'),
                        tax_data.get('status'),
                        tax_data.get('companyType'),
                        contact_data.get('email'),
                        final_phone,  # Sử dụng phone đã được xử lý
                        json.dumps({
                            'tax_info': tax_data, 
                            'contact_info': contact_data,
                            'phone_source': phone_source,
                            'final_phone': final_phone
                        }, ensure_ascii=False),
                        existing_record[0]
                    ))
                    
                    logger.info(f"Updated existing record for keyword: {keyword}, phone source: {phone_source}")
                    
                else:
                    # Thêm record mới
                    insert_query = """
                    INSERT INTO company_info 
                    (keyword, tax_id, company_name, address, legal_representative, 
                    start_date, status, company_type, email, phone, raw_data)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                    
                    cursor.execute(insert_query, (
                        keyword,
                        tax_data.get('taxID'),
                        tax_data.get('companyName'),
                        tax_data.get('address'),
                        tax_data.get('legalRepresentative'),
                        tax_data.get('startDate'),
                        tax_data.get('status'),
                        tax_data.get('companyType'),
                        contact_data.get('email'),
                        final_phone,  # Sử dụng phone đã được xử lý
                        json.dumps({
                            'tax_info': tax_data, 
                            'contact_info': contact_data,
                            'phone_source': phone_source,
                            'final_phone': final_phone
                        }, ensure_ascii=False)
                    ))
                    
                    logger.info(f"Inserted new record for keyword: {keyword}, phone source: {phone_source}")
                
                conn.commit()
                return True
                
        except Exception as e:
            logger.error(f"Error saving company info: {e}")
            return False
    
    def get_company_info(self, keyword=None, tax_id=None):
        """Lấy thông tin công ty từ database"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                if keyword:
                    query = "SELECT * FROM company_info WHERE keyword = ? ORDER BY created_at DESC"
                    cursor.execute(query, (keyword,))
                elif tax_id:
                    query = "SELECT * FROM company_info WHERE tax_id = ? ORDER BY created_at DESC"
                    cursor.execute(query, (tax_id,))
                else:
                    query = "SELECT * FROM company_info ORDER BY created_at DESC"
                    cursor.execute(query)
                
                columns = [column[0] for column in cursor.description]
                results = []
                
                for row in cursor.fetchall():
                    row_dict = dict(zip(columns, row))
                    # Parse raw_data JSON
                    if row_dict.get('raw_data'):
                        try:
                            row_dict['raw_data'] = json.loads(row_dict['raw_data'])
                        except:
                            pass
                    results.append(row_dict)
                
                return results
                
        except Exception as e:
            logger.error(f"Error getting company info: {e}")
            return []
# Add this to your FastAPI application to replace the existing DatabaseManager

import socket
import pyodbc
import logging
import time
import subprocess
import sys
from typing import Dict, Optional, Tuple

class FixedDatabaseManager:
    def __init__(self, config):
        self.config = config
        self.max_retries = 3
        self.retry_delay = 5
        self.logger = logging.getLogger(__name__)
    
    def _comprehensive_network_test(self) -> Tuple[bool, str]:
        """Test network connectivity thoroughly"""
        host = self.config['server']
        port = self.config['port']
        
        # Test 1: Basic ping
        try:
            if sys.platform.startswith('win'):
                result = subprocess.run(['ping', '-n', '2', host], 
                                      capture_output=True, text=True, timeout=15)
            else:
                result = subprocess.run(['ping', '-c', '2', host], 
                                      capture_output=True, text=True, timeout=15)
            
            ping_success = result.returncode == 0
            self.logger.info(f"Ping test: {'SUCCESS' if ping_success else 'FAILED'}")
            
        except Exception as e:
            ping_success = False
            self.logger.error(f"Ping failed: {e}")
        
        # Test 2: TCP connection with detailed error
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(15)
            
            start_time = time.time()
            result = sock.connect_ex((host, port))
            connection_time = time.time() - start_time
            
            sock.close()
            
            if result == 0:
                self.logger.info(f"TCP connection SUCCESS (took {connection_time:.2f}s)")
                return True, f"Network OK - Connection time: {connection_time:.2f}s"
            else:
                error_messages = {
                    10060: "Connection timed out",
                    10061: "Connection refused (service not running)",
                    10065: "Host unreachable",
                    10054: "Connection reset by peer",
                    11001: "Host not found"
                }
                
                error_msg = error_messages.get(result, f"Unknown error code {result}")
                self.logger.error(f"TCP connection FAILED: {error_msg}")
                return False, f"TCP connection failed: {error_msg}"
                
        except Exception as e:
            self.logger.error(f"TCP test exception: {e}")
            return False, f"TCP test failed: {str(e)}"
    
    def _test_connection_with_timeout(self, connection_string: str, timeout: int = 30) -> Optional[object]:
        """Test connection with specific timeout"""
        try:
            self.logger.info(f"Testing connection with {timeout}s timeout...")
            
            connection = pyodbc.connect(connection_string, timeout=timeout)
            
            # Test with a simple query
            cursor = connection.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchone()
            cursor.close()
            
            self.logger.info("✅ Connection test successful!")
            return connection
            
        except pyodbc.Error as e:
            error_code = e.args[0] if e.args else "Unknown"
            error_message = e.args[1] if len(e.args) > 1 else str(e)
            self.logger.error(f"Connection failed - Code: {error_code}, Message: {error_message}")
            return None
        except Exception as e:
            self.logger.error(f"Unexpected connection error: {e}")
            return None
    
    def get_connection(self):
        """Get connection with comprehensive error handling"""
        
        # Step 1: Network test
        self.logger.info("=== DATABASE CONNECTION DIAGNOSIS ===")
        network_ok, network_msg = self._comprehensive_network_test()
        
        if not network_ok:
            error_msg = f"""
            🚨 NETWORK CONNECTIVITY FAILED: {network_msg}
            
            TROUBLESHOOTING STEPS:
            1. Check Radmin VPN Status:
               - Is Radmin VPN running?
               - Are you connected to the correct network?
               - Can you ping other devices on the VPN network?
            
            2. Verify SQL Server:
               - Is SQL Server running on 26.25.148.0?
               - Is TCP/IP protocol enabled?
               - Is port 1433 open?
            
            3. Check Firewall:
               - Windows Firewall on both machines
               - Antivirus firewall
               - Router/network firewall
            
            4. Test from SQL Server Management Studio:
               - Try connecting with the same credentials
               - Server: 26.25.148.0,1433
               - Authentication: SQL Server
               - Username: sa
               - Password: 123
            """
            
            self.logger.error(error_msg)
            raise Exception(f"Network connectivity failed: {network_msg}")
        
        self.logger.info(f"✅ Network connectivity OK: {network_msg}")
        
        # Step 2: Test different connection approaches
        connection_variants = [
            # Standard connection
            (f"DRIVER={self.config['driver']};SERVER={self.config['server']},{self.config['port']};DATABASE={self.config['database']};UID={self.config['username']};PWD={self.config['password']};Encrypt=no;TrustServerCertificate=yes;Connection Timeout=30;", "Standard"),
            
            # Longer timeout
            (f"DRIVER={self.config['driver']};SERVER={self.config['server']},{self.config['port']};DATABASE={self.config['database']};UID={self.config['username']};PWD={self.config['password']};Encrypt=no;TrustServerCertificate=yes;Connection Timeout=60;Login Timeout=60;", "Long timeout"),
            
            # Without explicit port
            (f"DRIVER={self.config['driver']};SERVER={self.config['server']};DATABASE={self.config['database']};UID={self.config['username']};PWD={self.config['password']};Encrypt=no;TrustServerCertificate=yes;Connection Timeout=30;", "No port"),
            
            # Minimal connection string
            (f"DRIVER={self.config['driver']};SERVER={self.config['server']};DATABASE={self.config['database']};UID={self.config['username']};PWD={self.config['password']};", "Minimal"),
        ]
        
        for connection_string, variant_name in connection_variants:
            self.logger.info(f"Trying {variant_name} connection...")
            
            connection = self._test_connection_with_timeout(connection_string, 30)
            if connection:
                self.logger.info(f"✅ SUCCESS with {variant_name} connection!")
                return connection
        
        # If all attempts failed
        error_msg = """
        ❌ ALL CONNECTION ATTEMPTS FAILED
        
        This indicates that while network connectivity exists, there's likely an issue with:
        1. SQL Server authentication (check username/password)
        2. Database permissions
        3. SQL Server configuration
        4. ODBC driver issues
        
        Please verify:
        - SQL Server is accepting connections
        - 'sa' account is enabled and password is correct
        - SQL Server is configured for mixed mode authentication
        - TCP/IP protocol is enabled in SQL Server Configuration Manager
        """
        
        self.logger.error(error_msg)
        raise Exception("Database connection failed after all attempts. Check SQL Server configuration.")

# Update your FastAPI endpoint to use the fixed manager

# Fixed database connection test endpoint
@app.get("/test-db-fixed")
async def test_database_connection_fixed():
    """Enhanced database connection test with better diagnostics"""
    try:
        # Use the fixed database manager
        fixed_db_manager = FixedDatabaseManager(SQL_SERVER_CONFIG)
        
        with fixed_db_manager.get_connection() as conn:
            cursor = conn.cursor()
            
            # Get comprehensive server info with fixed SQL syntax
            cursor.execute("""
                SELECT 
                    @@VERSION as server_version,
                    DB_NAME() as database_name,
                    GETDATE() as server_time,
                    @@SERVERNAME as server_name,
                    @@SERVICENAME as service_name
            """)
            
            result = cursor.fetchone()
            
            # Test table operations
            cursor.execute("""
                SELECT COUNT(*) as table_count 
                FROM INFORMATION_SCHEMA.TABLES 
                WHERE TABLE_NAME = 'company_info'
            """)
            table_exists = cursor.fetchone()[0] > 0
            
            record_count = 0
            if table_exists:
                cursor.execute("SELECT COUNT(*) FROM company_info")
                record_count = cursor.fetchone()[0]
            
            return {
                "status": "success",
                "message": "Database connection successful with enhanced diagnostics",
                "server_info": {
                    "version": result[0],
                    "database": result[1],
                    "server_time": str(result[2]),  # Changed from current_time to server_time
                    "server_name": result[3],
                    "service_name": result[4]
                },
                "table_info": {
                    "company_info_exists": table_exists,
                    "record_count": record_count
                },
                "connection_config": {
                    "server": SQL_SERVER_CONFIG['server'],
                    "port": SQL_SERVER_CONFIG['port'],
                    "database": SQL_SERVER_CONFIG['database'],
                    "username": SQL_SERVER_CONFIG['username']
                }
            }
            
    except Exception as e:
        return {
            "status": "error",
            "message": "Database connection failed",
            "error": str(e),
            "troubleshooting": {
                "check_radmin_vpn": "Ensure Radmin VPN is connected",
                "check_sql_server": "Verify SQL Server is running on 26.25.148.0",
                "check_firewall": "Check firewall settings",
                "test_ssms": "Try connecting with SQL Server Management Studio"
            }
        }

# Replace your existing DatabaseManager with FixedDatabaseManager in your main code
# db_manager = FixedDatabaseManager(SQL_SERVER_CONFIG)
#        
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
    text = re.sub(r'\s+', ' ', text)
    
    # Trích xuất mã số doanh nghiệp
    tax_code = ""
    for pattern in [r'Mã số doanh nghiệp:\s*(\d{10})', r'Mã số doanh nghiệp\s*(\d{10})', r'(?:^|\s)(\d{10})(?=\s|$)']:
        match = re.search(pattern, text, re.MULTILINE)
        if match:
            tax_code = match.group(1)
            break
    
    # Trích xuất điện thoại
    phone_pattern = r'(?:Điện thoại:\s*|Tel:\s*|Phone:\s*)?(\d{2,4}[\.\-\s]?\d{3,4}[\.\-\s]?\d{3,4}[\.\-\s]?\d{0,4}|\d{9,11})'
    valid_prefixes = ['01', '02', '03', '05', '07', '08', '09', '024', '028', '0236', '0256', '0274', '0204', '84']
    
    for match in sorted(re.finditer(phone_pattern, text, re.MULTILINE | re.IGNORECASE), key=lambda x: x.start()):
        phone = match.group(1)
        clean_phone = re.sub(r'[\.\-\s]', '', phone)
        
        # Tránh nhầm với mã số thuế
        if clean_phone == tax_code or (tax_code and (clean_phone.startswith(tax_code) or tax_code.startswith(clean_phone))):
            continue
        
        # Kiểm tra độ dài và prefix hợp lệ
        if 9 <= len(clean_phone) <= 11:
            if any(clean_phone.startswith(prefix) for prefix in valid_prefixes) or (clean_phone.startswith('0') and len(clean_phone) in [10, 11]):
                info['phone'] = phone
                break
    
    # Trích xuất email
    for pattern in [r'Email:\s*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', 
                   r'Email\s*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', 
                   r'([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})']:
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
db_manager = DatabaseManager(SQL_SERVER_CONFIG)

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
    """Fixed crawl function with correct Playwright syntax"""
    registration_types = [('NEW', 'Đăng ký mới'), ('AMEND', 'Đăng ký thay đổi')]
    
    for attempt in range(max_retries):
        browser = None
        try:
            logger.info(f"Attempt {attempt + 1}/{max_retries} for MST: {mst}")
            
            async with async_playwright() as p:
                # Cấu hình browser
                browser = await p.chromium.launch(
                    headless=True,
                    args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', 
                          '--disable-blink-features=AutomationControlled', '--disable-web-security',
                          '--disable-features=VizDisplayCompositor',
                          '--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36']
                )
                
                context = await browser.new_context(
                    viewport={'width': 1366, 'height': 768},
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    extra_http_headers={
                        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                        'Accept-Language': 'vi-VN,vi;q=0.9,en;q=0.8',
                        'Connection': 'keep-alive'
                    },
                    ignore_https_errors=True, java_script_enabled=True, bypass_csp=True
                )
                
                context.set_default_timeout(180000)
                await context.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "media", "font"] else route.continue_())
                
                page = await context.new_page()
                page.set_default_timeout(180000)
                page.on("pageerror", lambda error: logger.error(f"Page error: {error}"))
                page.on("console", lambda msg: logger.info(f"Console: {msg.text}"))
                
                # Thử từng loại đăng ký
                for reg_type, reg_name in registration_types:
                    logger.info(f"Trying registration type: {reg_name} ({reg_type})")
                    
                    try:
                        # Navigate với retry
                        for nav_attempt in range(3):
                            try:
                                response = await page.goto(TARGET_URL, timeout=180000, wait_until='networkidle')
                                if response and response.status >= 400:
                                    raise Exception(f"HTTP {response.status} error")
                                await page.wait_for_timeout(5000)
                                break
                            except Exception as nav_error:
                                if nav_attempt == 2:
                                    raise nav_error
                                await asyncio.sleep(3)
                        
                        # Wait for form elements
                        selectors = ['#ctl00_C_ANNOUNCEMENT_TYPE_IDFilterFld', 'select[name*="ANNOUNCEMENT_TYPE"]', 'select[id*="ANNOUNCEMENT_TYPE"]']
                        form_found = False
                        for selector in selectors:
                            try:
                                await page.wait_for_selector(selector, timeout=30000)
                                form_found = True
                                break
                            except:
                                continue
                        
                        if not form_found:
                            raise Exception("Form elements not found on page")
                        
                        # Fill form
                        await page.select_option('#ctl00_C_ANNOUNCEMENT_TYPE_IDFilterFld', reg_type)
                        await page.wait_for_timeout(3000)
                        await page.fill('#ctl00_C_ENT_GDT_CODEFld', mst)
                        await page.wait_for_timeout(2000)
                        
                        # Solve captcha
                        captcha_code = None
                        for captcha_attempt in range(2):
                            try:
                                captcha_code = await asyncio.to_thread(solver.solve_recaptcha, SITE_KEY, TARGET_URL)
                                break
                            except Exception as captcha_error:
                                if captcha_attempt == 1:
                                    raise captcha_error
                                await asyncio.sleep(5)
                        
                        if not captcha_code or not await inject_captcha_response(page, captcha_code):
                            raise Exception("Failed to solve/inject captcha")
                        
                        await page.wait_for_timeout(3000)
                        
                        # Submit form
                        await page.click('#ctl00_C_BtnFilter')
                        await page.wait_for_load_state('networkidle', timeout=120000)
                        
                        # Check for results
                        try:
                            await page.wait_for_selector('#ctl00_C_CtlList', timeout=45000)
                        except:
                            # Check for no results
                            no_results_selectors = ['text=Không tìm thấy dữ liệu', 'text=No data found', '.no-results', '[id*="NoData"]']
                            for selector in no_results_selectors:
                                try:
                                    await page.wait_for_selector(selector, timeout=5000)
                                    logger.info(f"No results found for {reg_name}")
                                    break
                                except:
                                    continue
                            else:
                                raise Exception("Results table not found")
                            continue
                        
                        # Find PDF button
                        pdf_selectors = ['input[id^="ctl00_C_CtlList_"][id$="_LnkGetPDFActive"]', 'a[href*="pdf"]', 'input[value*="PDF"]', 'button[onclick*="pdf"]']
                        pdf_button = None
                        for selector in pdf_selectors:
                            try:
                                pdf_button = await page.query_selector(selector)
                                if pdf_button:
                                    break
                            except:
                                continue
                        
                        if not pdf_button:
                            if reg_type == registration_types[-1][0]:
                                return None
                            continue
                        
                        # Download PDF
                        file_name = f"{mst}_{reg_type}_{uuid.uuid4().hex[:8]}.pdf"
                        download_path = os.path.join(os.getcwd(), file_name)
                        
                        try:
                            async with page.expect_download(timeout=120000) as download_info:
                                await pdf_button.click()
                            
                            download = await download_info.value
                            await download.save_as(download_path)
                            
                            if os.path.exists(download_path) and os.path.getsize(download_path) > 0:
                                logger.info(f"Successfully downloaded PDF from {reg_name}")
                                return file_name
                            else:
                                if reg_type == registration_types[-1][0]:
                                    return None
                                continue
                                
                        except Exception as download_error:
                            if reg_type == registration_types[-1][0]:
                                raise Exception(f"PDF download failed for all registration types. Last error: {download_error}")
                            continue
                    
                    except Exception as reg_error:
                        if reg_type == registration_types[-1][0]:
                            raise reg_error
                        continue
                
                return None
                
        except Exception as e:
            logger.error(f"Attempt {attempt + 1} failed: {e}")
            if attempt == max_retries - 1:
                raise Exception(f"All {max_retries} attempts failed. Last error: {e}")
            
            await asyncio.sleep(2 ** attempt)
            
        finally:
            if browser:
                try:
                    await browser.close()
                except:
                    pass


@app.get("/tax-info")
async def get_tax_info_api(keyword: str = Query(..., min_length=1, description="Keyword to search for tax information")):
    """API endpoint để scrape thông tin thuế từ masothue.com"""
    browser = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-blink-features=AutomationControlled', 
                      '--disable-images', '--disable-extensions', '--disable-plugins']
            )
            
            context = await browser.new_context(
                viewport={'width': 1280, 'height': 800},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114 Safari/537.36'
            )
            
            page = await context.new_page()
            await page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "font", "media"] else route.continue_())
            
            logger.info(f"Navigating to masothue.com for keyword: {keyword}")
            
            # Truy cập và tìm kiếm
            await page.goto('https://masothue.com', timeout=60000)
            await page.wait_for_selector('input[name="q"]', timeout=60000)
            await page.fill('input[name="q"]', keyword)
            await page.click('.btn-search-submit')
            await page.wait_for_selector('table.table-taxinfo tbody', timeout=60000)
            
            # Trích xuất dữ liệu
            result = await page.evaluate("""
                () => {
                    const result = {};
                    const companyNameHeader = document.querySelector('table.table-taxinfo thead th[itemprop="name"] .copy');
                    if (companyNameHeader) {
                        result.companyName = companyNameHeader.getAttribute('title') || companyNameHeader.innerText.trim();
                    }
                    
                    const labelMap = {
                        'Mã số thuế': 'taxID',
                        'Địa chỉ': 'address', 
                        'Người đại diện': 'legalRepresentative',
                        'Ngày hoạt động': 'startDate',
                        'Tình trạng': 'status',
                        'Loại hình DN': 'companyType'
                    };
                    
                    Array.from(document.querySelectorAll('table.table-taxinfo tbody tr')).forEach(row => {
                        const label = row.querySelector('td:first-child')?.innerText.trim();
                        const valueCell = row.querySelector('td:nth-child(2)');
                        if (!label || !valueCell) return;
                        
                        let value = valueCell.innerText.trim();
                        if (label.includes('Người đại diện')) {
                            const nameElement = valueCell.querySelector('[itemprop="name"]');
                            if (nameElement) value = nameElement.innerText.trim();
                        }
                        
                        Object.keys(labelMap).forEach(key => {
                            if (label.includes(key)) result[labelMap[key]] = value;
                        });
                    });
                    
                    return result;
                }
            """)
            
            logger.info(f"Successfully scraped tax info for keyword: {keyword}")
            return JSONResponse({"keyword": keyword, "data": result, "status": "success"})
            
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
    """Enhanced API endpoint với phone priority logic: masothue.com phone first, then PDF phone"""
    try:
        # Step 1: Get tax info with retry
        logger.info(f"Step 1: Getting tax info for keyword: {keyword}")
        tax_info = await get_tax_info_internal(keyword, max_retries=3)
        
        if not tax_info or not tax_info.get('data'):
            return JSONResponse({"error": "No tax information found for the given keyword", "keyword": keyword}, status_code=404)
        
        # Get MST from tax_info
        mst = tax_info['data'].get('taxID')
        if not mst:
            return JSONResponse({"error": "Tax ID not found in tax information", "keyword": keyword, "tax_info": tax_info}, status_code=404)
        
        logger.info(f"Found MST: {mst}")
        
        # Step 2: Get contact info with retry
        logger.info(f"Step 2: Getting contact info for MST: {mst}")
        contact_info = await get_contact_info_internal(mst, max_retries=3)
        
        # Step 3: Combine results with phone priority logic
        tax_data = tax_info['data']
        pdf_contact = contact_info if contact_info else {}

        # Logic ưu tiên: phone từ masothue.com, nếu không có thì lấy từ PDF
        final_phone = tax_data.get('phone') or pdf_contact.get('phone')
        phone_source = "masothue.com" if tax_data.get('phone') else ("pdf" if pdf_contact.get('phone') else None)
        
        logger.info(f"Using phone from {phone_source}: {final_phone}" if final_phone else "No phone number found from any source")

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
        
        # Step 4: Save to database
        try:
            db_saved = db_manager.save_company_info(keyword=keyword, tax_info=tax_data, contact_info=final_contact_info)
            combined_result["database_status"] = "saved" if db_saved else "failed"
            logger.info(f"Database save {'successful' if db_saved else 'failed'} for keyword: {keyword}")
        except Exception as db_error:
            logger.error(f"Database save error: {db_error}")
            combined_result.update({"database_status": "error", "database_error": str(db_error)})
        
        logger.info(f"Successfully combined information for keyword: {keyword}")
        return JSONResponse(combined_result)
        
    except Exception as e:
        logger.error(f"Combined API error: {e}")
        return JSONResponse({"error": f"Failed to get combined information: {str(e)}", "keyword": keyword}, status_code=500)
    
async def get_tax_info_internal(keyword: str, max_retries: int = 3):
    """Simplified tax info function with correct Playwright syntax"""
    
    for attempt in range(max_retries):
        browser = None
        try:
            logger.info(f"Tax info attempt {attempt + 1}/{max_retries} for keyword: {keyword}")
            
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', 
                          '--disable-extensions', '--disable-plugins', '--single-process']
                )
                
                context = await browser.new_context(
                    viewport={'width': 1280, 'height': 800},
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                )
                context.set_default_timeout(60000)
                
                page = await context.new_page()
                page.set_default_timeout(60000)
                
                # Block unnecessary resources
                await page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "font", "media"] else route.continue_())
                
                # Navigate and search
                await page.goto('https://masothue.com', timeout=60000)
                await page.wait_for_load_state('domcontentloaded')
                await page.wait_for_selector('input[name="q"]', timeout=60000)
                await page.fill('input[name="q"]', keyword)
                await page.click('.btn-search-submit')
                await page.wait_for_load_state('domcontentloaded')
                await page.wait_for_selector('table.table-taxinfo tbody', timeout=60000)
                
                # Extract data
                result = await page.evaluate("""
                () => {
                    const result = {};
                    
                    // Get company name
                    const companyNameHeader = document.querySelector('table.table-taxinfo thead th[itemprop="name"] .copy');
                    if (companyNameHeader) {
                        result.companyName = companyNameHeader.getAttribute('title') || companyNameHeader.innerText.trim();
                    }
                    
                    // Get info from tbody
                    const rows = Array.from(document.querySelectorAll('table.table-taxinfo tbody tr'));
                    
                    rows.forEach(row => {
                        const label = row.querySelector('td:first-child')?.innerText.trim();
                        const valueCell = row.querySelector('td:nth-child(2)');
                        if (!label || !valueCell) return;
                        
                        let value = valueCell.innerText.trim();
                        
                        // Handle phone number extraction
                        if (label.includes('Điện thoại')) {
                            const phoneElement = valueCell.querySelector('span.copy');
                            if (phoneElement) {
                                const phoneNumber = phoneElement.getAttribute('title') || phoneElement.innerText.trim();
                                if (phoneNumber && !phoneNumber.includes('Bị ẩn') && !phoneNumber.includes('*') && phoneNumber.length > 5) {
                                    result.phone = phoneNumber;
                                }
                            } else {
                                const phoneMatch = value.match(/(\d{2,4}[\.\-\s]?\d{3,4}[\.\-\s]?\d{3,4}[\.\-\s]?\d{0,4}|\d{9,11})/);
                                if (phoneMatch && !value.includes('Bị ẩn') && !value.includes('*')) {
                                    result.phone = phoneMatch[1];
                                }
                            }
                        }
                        
                        // Handle representative name
                        if (label.includes('Người đại diện')) {
                            const nameElement = valueCell.querySelector('[itemprop="name"]');
                            if (nameElement) value = nameElement.innerText.trim();
                        }
                        
                        // Map other fields
                        const fieldMap = {
                            'Mã số thuế': 'taxID',
                            'Địa chỉ': 'address', 
                            'Người đại diện': 'legalRepresentative',
                            'Ngày hoạt động': 'startDate',
                            'Tình trạng': 'status',
                            'Loại hình DN': 'companyType'
                        };
                        
                        Object.entries(fieldMap).forEach(([key, field]) => {
                            if (label.includes(key)) result[field] = value;
                        });
                    });
                    
                    return result;
                }
                """)
                
                logger.info(f"Successfully scraped tax info for keyword: {keyword}")
                return {"keyword": keyword, "data": result, "status": "success"}
                
        except Exception as e:
            logger.error(f"Tax info attempt {attempt + 1} failed: {e}")
            if attempt == max_retries - 1:
                raise Exception(f"Failed to fetch tax info after {max_retries} attempts: {str(e)}")
            
            await asyncio.sleep(2 ** attempt)  # Exponential backoff
                
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
@app.get("/test-db")
async def test_database_connection():
    """
    API endpoint để test kết nối database
    """
    try:
        # Test connection
        with db_manager.get_connection() as conn:
            cursor = conn.cursor()
            
            # Test query
            cursor.execute("SELECT 1")
            result = cursor.fetchone()
            
            # Get database info
            cursor.execute("SELECT DB_NAME() as database_name")
            db_info = cursor.fetchone()
            
            # Get server info
            cursor.execute("SELECT @@VERSION as server_version")
            server_info = cursor.fetchone()
            
            # Test table existence
            cursor.execute("""
                SELECT COUNT(*) as table_count 
                FROM INFORMATION_SCHEMA.TABLES 
                WHERE TABLE_NAME = 'company_info'
            """)
            table_exists = cursor.fetchone()[0] > 0
            
            # Get record count if table exists
            record_count = 0
            if table_exists:
                cursor.execute("SELECT COUNT(*) FROM company_info")
                record_count = cursor.fetchone()[0]
            
            return JSONResponse({
                "status": "success",
                "message": "Database connection successful",
                "database_name": db_info[0] if db_info else "Unknown",
                "server_version": server_info[0] if server_info else "Unknown",
                "table_exists": table_exists,
                "record_count": record_count,
                "connection_config": {
                    "server": SQL_SERVER_CONFIG['server'],
                    "database": SQL_SERVER_CONFIG['database'],
                    "username": SQL_SERVER_CONFIG['username'],
                    "port": SQL_SERVER_CONFIG['port']
                }
            })
            
    except Exception as e:
        logger.error(f"Database connection test failed: {e}")
        return JSONResponse({
            "status": "error",
            "message": "Database connection failed",
            "error": str(e),
            "connection_config": {
                "server": SQL_SERVER_CONFIG['server'],
                "database": SQL_SERVER_CONFIG['database'],
                "username": SQL_SERVER_CONFIG['username'],
                "port": SQL_SERVER_CONFIG['port']
            }
        }, status_code=500)

@app.get("/test-db2")
async def test_database():
    try:
        db_manager = DatabaseManager(SQL_SERVER_CONFIG)
        
        # Test TCP connection
        tcp_test = db_manager._test_connection()
        if not tcp_test:
            return {"status": "error", "message": "TCP connection failed"}
        
        # Test database connection
        with db_manager.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT @@VERSION")
            version = cursor.fetchone()[0]
            cursor.close()
            
        return {
            "status": "success", 
            "message": "Database connection successful",
            "server_version": version,
            "config": {
                "server": SQL_SERVER_CONFIG['server'],
                "port": SQL_SERVER_CONFIG['port'],
                "database": SQL_SERVER_CONFIG['database']
            }
        }
        
    except Exception as e:
        return {"status": "error", "message": str(e)}
    


# Test connection script
def test_connection():
    config = {
        'server': '26.25.148.0',  # Radmin VPN IP
        'database': 'CompanyDB',
        'username': 'sa',
        'password': '123',
        'driver': '{ODBC Driver 17 for SQL Server}',
        'port': 1433
    }
    
    connection_string = (
        f"DRIVER={config['driver']};"
        f"SERVER={config['server']},{config['port']};"
        f"DATABASE={config['database']};"
        f"UID={config['username']};"
        f"PWD={config['password']};"
        f"Encrypt=no;"
        f"TrustServerCertificate=yes;"
        f"Connection Timeout=30;"
    )
    
    try:
        print("Testing connection...")
        conn = pyodbc.connect(connection_string)
        cursor = conn.cursor()
        
        # Test query
        cursor.execute("SELECT @@VERSION, DB_NAME(), GETDATE()")
        result = cursor.fetchone()
        
        print("✅ Connection successful!")
        print(f"Server version: {result[0]}")
        print(f"Database: {result[1]}")
        print(f"Current time: {result[2]}")
        
        cursor.close()
        conn.close()
        return True
        
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        return False

    
if __name__ == "__main__":
    test_connection()
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port)

