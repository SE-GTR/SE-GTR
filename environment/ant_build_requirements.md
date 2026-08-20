# Ant build requirements

## Java

- **JDK 8** (any 1.8.x vendor). SE-GTR targets Java 8 bytecode because
  SF110 projects were generated under Java 8 and EvoSuite 1.2.0's
  runtime JAR is compiled for `-source 1.8`.
- JAVA_HOME must point at a JDK, not a JRE.

## Ant

- **Apache Ant 1.10+**.
- Each SF110 project ships with a `build.xml` that expects the
  following Ant targets to exist: `clean`, `compile`,
  `compile-evosuite`. The targets are defined in the pristine SF110
  `build.xml` files; SE-GTR does not modify them.

## Required jars

Placed under a shared directory (set `shared_lib_dir` in the YAML config):

- `evosuite-standalone-runtime-1.2.0.jar` — from
  https://www.evosuite.org/downloads/
- `junit-4.11.jar` and `hamcrest-core-1.3.jar` — Maven Central
- `smelly-1.0-shaded.jar` — Smelly-E fork (see paper's citation);
  rebuild the shaded jar with `mvn package` in the Smelly-E checkout

None of the above are bundled in this package — download each from
the upstream release and drop them into the `shared_lib_dir` you
configure.

## PIT

- **PIT 1.17.4** (`pitest-command-line`, `pitest`, `pitest-ant-plugin`).
- Additional dependencies for PIT: `commons-lang3`, `commons-text`.
- Driven by `00_code/scripts/run_phase4_pit.py`, which invokes
  `pitest-command-line` via `java -jar` and produces `score.json` +
  `mutations.xml` per project.

## Resource limits

- Phase-4 main run used 90-minute wall-clock per project, retried at
  180-minute for 8 stragglers (of which 6 finished).
- PIT runs use a 30-minute wall-clock per project
  (`run_phase4_pit.py`, `TIMEOUT_SEC = 30 * 60`; `phaseE_run_pit.py`,
  `--timeout-min` default `30`).
- Memory: `-Xmx4g` is sufficient for every SF110 project in the cohort.
