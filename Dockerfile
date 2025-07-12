FROM python:3.11-slim

# Cài đặt các dependencies hệ thống cần thiết
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    ca-certificates \
    procps \
    curl \
    unixodbc \
    unixodbc-dev \
    odbcinst1debian2 \
    libodbc1 \
    apt-transport-https \
    lsb-release \
    && rm -rf /var/lib/apt/lists/*

# Cài đặt Microsoft ODBC Driver 17 và 18 for SQL Server
RUN curl https://packages.microsoft.com/keys/microsoft.asc | apt-key add - \
    && curl https://packages.microsoft.com/config/debian/11/prod.list > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y msodbcsql17 msodbcsql18 \
    && rm -rf /var/lib/apt/lists/*

# Cài đặt Chrome dependencies
RUN apt-get update && apt-get install -y \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libwayland-client0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    xdg-utils \
    libu2f-udev \
    libvulkan1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements và cài đặt Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Cài đặt Playwright browsers
RUN playwright install chromium
RUN playwright install-deps

# Copy source code
COPY . .

# Kiểm tra ODBC drivers đã cài đặt
RUN odbcinst -q -d

# Expose port
EXPOSE 8000

# Command to run
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
