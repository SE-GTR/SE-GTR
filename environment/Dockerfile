# Minimal image for running SE-GTR repro.
# Build:  docker build -t segtr-repro -f 07_environment/Dockerfile .
# Run:    docker run --rm -it -e LLM_API_KEY -e LLM_BASE_URL segtr-repro bash
FROM eclipse-temurin:8-jdk-jammy

# Ant + Python + build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
        ant \
        python3.11 \
        python3-pip \
        wget \
        ca-certificates \
        maven \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /segtr
COPY 00_code/segtr/requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

# Download third-party jars into tools/
# (the image does not bundle these; add wget steps here if you want them baked in)
#   - evosuite-standalone-runtime-1.2.0.jar
#   - junit-4.11.jar, hamcrest-core-1.3.jar
#   - Smelly-E shaded jar (build from upstream)

# PIT 1.17.4
ENV PIT_VERSION=1.17.4
RUN mkdir -p tools/pit && \
    cd tools/pit && \
    wget -q https://repo1.maven.org/maven2/org/pitest/pitest-command-line/${PIT_VERSION}/pitest-command-line-${PIT_VERSION}.jar && \
    wget -q https://repo1.maven.org/maven2/org/pitest/pitest/${PIT_VERSION}/pitest-${PIT_VERSION}.jar && \
    wget -q https://repo1.maven.org/maven2/org/pitest/pitest-ant-plugin/${PIT_VERSION}/pitest-ant-plugin-${PIT_VERSION}.jar

COPY . /segtr/

CMD ["bash"]
