import pyodbc

try:
    connection_string = (
        "DRIVER={ODBC Driver 17 for SQL Server};"
        "SERVER=26.25.148.0,1433;"
        "DATABASE=CompanyDB;"
        "UID=sa;"
        "PWD=123;"
        "TrustServerCertificate=yes;"
        "Connection Timeout=30;"
    )
    
    connection = pyodbc.connect(connection_string)
    print("Connection successful!")
    connection.close()
    
except Exception as e:
    print(f"Connection failed: {e}")