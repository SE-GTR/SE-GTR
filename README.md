# SE-GTR: Repairing Quality Issues Specific to Automatically Generated Unit Tests

This repository contains the artifacts and scripts for the paper:
**“Repairing Quality Issues Specific to Automatically Generated Unit Tests”** (ISSTA 2026).

We refer to the overall approach as **SE-GTR** and the smell detection tool as **Smelly-E**.

> **Anonymity note**: This package has been sanitized for double-blind review. All paths in scripts/examples use placeholders such as `/PATH/TO/REPO` and `/PATH/TO/SF110`.

---

## 1) Repository Purpose and Scope

- **Goal**: Provide a reproducible pipeline for repairing quality issues in EvoSuite-generated JUnit tests using deterministic rules + LLM-based transformations.
- **What is included**:
  - Final analysis artifacts (CSV/JSON) under `output/analysis/*`.
  - Patch diffs and Smelly-E outputs under `output/by_project/*`.
  - Scripts to regenerate analysis tables from outputs.
  - Tools needed for Smelly-E evidence extraction and coverage/mutation measurement.

---

## 2) Folder Structure (Key Paths)

```
/REPO
  README.md
  requirements.txt
  pyproject.toml

  smell_repair/                 # core pipeline implementation
  smells/                       # 13 smell definitions

  tools/
    smelly-evidence/             # Smelly-E (evidence-enabled) JARs and helpers
    jacoco/                      # JaCoCo agent + CLI
    pit/                         # PIT CLI and dependencies
    minlib/, compat/             # minimal runtime/compat libs

  scripts/
    analysis/
      exec/      # RQ1 executability analysis
      smell/     # RQ2 smell reduction analysis
      qual/      # qualitative summaries
      jacoco/    # coverage summaries/deltas
      pit/       # PIT summaries/deltas
    metrics/
      jacoco/    # JaCoCo collection scripts
      pit/       # PIT collection scripts
      common.py

  output/
    analysis/
      exec/      # compile/validity/test rates + failure distributions
      smell/     # smell counts/deltas/densities
      qual/      # qualitative summaries
      jacoco/    # coverage summaries/deltas
      pit/       # PIT summaries/deltas
    by_project/  # per-project outputs (smelly_*.json, patches, smelly_after_*.json)
    excluded/    # excluded projects (if any)
    smelly_debug/
```

---

## 3) Environment Requirements

- **Python**: 3.9+ recommended
- **Java**: compatible with EvoSuite 1.2.0 and SF110 build scripts (Java 8/11 commonly used)
- **Ant**: for compiling SF110 projects
- **OS**: Linux/macOS recommended (Windows paths in examples are avoided)

Python deps (minimal):
```bash
pip install -r requirements.txt
```

---

## 4) LLM Configuration

SE-GTR expects an **OpenAI-compatible Chat Completions endpoint**.
Configure the LLM in `configs/config.yaml` under the `llm` section:

```yaml
llm:
  base_url: "https://YOUR-LLM-ENDPOINT/v1"
  api_key: "YOUR_API_KEY"
  model: "PROVIDER/MODEL_ID"
  temperature: 0.2
  top_p: 0.9
  max_tokens: 2048
  request_timeout_sec: 300
```

Notes:
- `model` is provider-specific (e.g., `"openai/gpt-oss-120b"` in our configs).
- Keep API keys **out of the repo** (use local config files or environment variables).

---

## 5) Data Preparation

You need the SF110 projects and EvoSuite tests for full reproduction.

**Required external data (not bundled here):**
- SF110 source projects (e.g., `/PATH/TO/SF110`)
- EvoSuite-generated tests under each project (as in SF110 distribution)

If you only want to **recompute tables from the included outputs**, you do NOT need SF110 sources.

---

## 6) Tool Preparation

### Smelly-E evidence extraction (optional but recommended)
We include an evidence-enabled Smelly-E build under:
```
/tools/smelly-evidence/
```
Use it to generate the extended smell JSON with evidence.

Example (paths are placeholders):
```bash
java -jar /PATH/TO/REPO/tools/smelly-evidence/smelly-1.0-shaded.jar \
  --detectors 0 \
  --evosuitePath /PATH/TO/REPO/tools/smelly-evidence/evosuite-standalone-runtime-1.2.0.jar \
  --junitPath /PATH/TO/REPO/tools/smelly-evidence/junit-4.11.jar \
  --mode 0 \
  --outputFilePath /PATH/TO/OUT \
  --outputFileName sf110_smelly_extended.json \
  --sourcePath /PATH/TO/SF110 \
  --testPath /PATH/TO/SF110 \
  -s " " \
  --resumeAnalisis false
```

### JaCoCo
Ensure these exist (already bundled):
```
/tools/jacoco/jacocoagent.jar
/tools/jacoco/jacococli.jar
```

### PIT
Ensure PIT CLI jars exist (already bundled):
```
/tools/pit/*.jar
```

---

## 7) Pipeline Execution (Full Reproduction)

**Note**: Full reproduction requires SF110 sources + EvoSuite tests.

### 6.1 Configure
Copy and edit `configs/config.example.yaml` → `configs/config.yaml`.
Use placeholder paths in the template and replace them with your local paths.

### 6.2 Run SE-GTR
```bash
python -m smell_repair.cli run \
  --config /PATH/TO/REPO/configs/config.yaml \
  --projects-root /PATH/TO/SF110 \
  --smelly-json /PATH/TO/SMELLY/sf110_smelly_extended.json \
  --out-root /PATH/TO/OUT
```

### 6.3 Outputs
Per project:
```
output/by_project/<project>/
  smelly_<project>.json                  # baseline smells (input)
  run_YYYYMMDD_*/
    patches/                             # applied diffs
    reports/smelly_after_<project>.json  # post-repair smells
```

---

## 8) Analysis Reproduction (from included outputs)

The package already includes analysis-ready CSV/JSON under:
```
output/analysis/{exec,smell,qual,jacoco,pit}/
```

You can recompute these tables with the scripts under `scripts/analysis/*`.
Examples (paths are placeholders):

```bash
# RQ1: executability
python /PATH/TO/REPO/scripts/analysis/exec/exec_compile_rate_all.py \
  --root /PATH/TO/REPO/output/by_project \
  --out /PATH/TO/REPO/output/analysis/exec/compile_success_rate.csv

python /PATH/TO/REPO/scripts/analysis/exec/exec_validity_rate_all.py \
  --root /PATH/TO/REPO/output/by_project \
  --out /PATH/TO/REPO/output/analysis/exec/validity_gate_success_rate.csv

python /PATH/TO/REPO/scripts/analysis/exec/exec_failure_dist_all.py \
  --root /PATH/TO/REPO/output/by_project \
  --out /PATH/TO/REPO/output/analysis/exec/failure_dist.csv

# RQ2: smell reduction
python /PATH/TO/REPO/scripts/analysis/smell/smell_reduction_all.py \
  --root /PATH/TO/REPO/output/by_project \
  --out /PATH/TO/REPO/output/analysis/smell/smell_counts.csv

python /PATH/TO/REPO/scripts/analysis/smell/smell_reduction_rate.py \
  --root /PATH/TO/REPO/output/by_project \
  --out /PATH/TO/REPO/output/analysis/smell/smell_reduction_rate.csv

# Qualitative summaries
python /PATH/TO/REPO/scripts/analysis/qual/qual_smell_report.py \
  --analysis-exec /PATH/TO/REPO/output/analysis/exec \
  --analysis-smell /PATH/TO/REPO/output/analysis/smell \
  --analysis-qual /PATH/TO/REPO/output/analysis/qual
```

---

## 9) Coverage & Mutation (Optional)

### JaCoCo collection
```bash
python /PATH/TO/REPO/scripts/metrics/jacoco/jacoco_all_parallel.py \
  --mode before \
  --projects-root /PATH/TO/SF110 \
  --out-root /PATH/TO/REPO/output/analysis/jacoco/before \
  --jacoco-agent /PATH/TO/REPO/tools/jacoco/jacocoagent.jar \
  --jacoco-cli /PATH/TO/REPO/tools/jacoco/jacococli.jar \
  --workers 6 \
  --continue-on-error
```

### PIT collection
```bash
python /PATH/TO/REPO/scripts/metrics/pit/pit_before_all.py \
  --projects-root /PATH/TO/SF110 \
  --out-root /PATH/TO/REPO/output/analysis/pit/before \
  --pitest-home /PATH/TO/REPO/tools/pit \
  --workers 6 \
  --continue-on-error
```

---

## 10) Troubleshooting / Known Issues

- **Missing SF110 data**: Full reproduction requires SF110 sources + EvoSuite tests.
- **Classpath issues / SLF4J bindings**: Some SF110 projects include conflicting logger bindings.
- **Native/JNI errors**: A subset of projects require native libraries (excluded from some analyses).
- **Timeouts**: Large projects may time out during compilation or test execution.
- **PIT failures**: PIT may fail if tests are not green; intersection-based evaluation is recommended.

---

## 11) Contact / Anonymity

This package is anonymized for double-blind review. Please avoid attempting to identify authors.
