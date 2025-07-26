FROM python:3.11-slim

# Install OCR dependencies and Playwright system libraries
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        tesseract-ocr \
        libtesseract-dev \
        libleptonica-dev \
        gcc \
        libjpeg-dev \
        zlib1g-dev \
        libpng-dev \
        libopenjp2-7-dev \
        libtiff5-dev \
        libwebp-dev \
        libnss3 \
        libatk-bridge2.0-0 \
        libgtk-3-0 \
        libxss1 \
        libasound2 \
        libgbm1 \
        libxshmfence1 \
        libxcomposite1 \
        libxrandr2 \
        libu2f-udev \
        libdrm2 \
        libxdamage1 \
        libxfixes3 \
        libjpeg-dev \
        ca-certificates \
        fonts-liberation \
        libappindicator3-1 \
        libcups2 \
        libdbus-1-3 \
        libnspr4 \
        lsb-release \
        xdg-utils \
        wget \
        curl \
        bash && \
    rm -rf /var/lib/apt/lists/*


# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers
RUN python -m playwright install

# Copy your project files
WORKDIR /app
COPY . .

ENTRYPOINT [ "python3", "navarro_pw.py" ]
