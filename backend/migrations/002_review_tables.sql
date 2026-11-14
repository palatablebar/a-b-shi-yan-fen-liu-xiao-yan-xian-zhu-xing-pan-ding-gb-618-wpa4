-- 上线评审三张表（运行时由 SQLAlchemy Base.metadata.create_all 建表，此文件为参考口径）。
-- review_runs 的自然键 (experiment_id, variant_code, data_fingerprint) 保证评审幂等：
-- 同一批数据重跑命中自然键，不新增行也不覆盖原结论。
-- review_releases 每实验一行：判定与依据（run_id）在同一事务里一次性切换。

CREATE TABLE IF NOT EXISTS review_plans (
  id INT AUTO_INCREMENT PRIMARY KEY,
  experiment_id INT NOT NULL,
  primary_metric VARCHAR(40) NOT NULL,
  guardrail_metrics JSON NULL,
  required_channels JSON NULL,
  alpha FLOAT NOT NULL DEFAULT 0.05,
  note TEXT NULL,
  registered_by VARCHAR(160) NOT NULL DEFAULT '',
  registered_at DATETIME NOT NULL,
  seed_key VARCHAR(120) NULL UNIQUE,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  UNIQUE KEY uq_review_plan_experiment (experiment_id),
  CONSTRAINT fk_review_plan_experiment FOREIGN KEY (experiment_id) REFERENCES experiments(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS review_runs (
  id INT AUTO_INCREMENT PRIMARY KEY,
  experiment_id INT NOT NULL,
  variant_code VARCHAR(40) NOT NULL,
  variant_name VARCHAR(120) NOT NULL DEFAULT '',
  data_fingerprint VARCHAR(64) NOT NULL,
  overall JSON NULL,
  channel_results JSON NULL,
  guardrail_results JSON NULL,
  verdict VARCHAR(16) NOT NULL,
  blockers JSON NULL,
  plan_snapshot JSON NULL,
  computed_by VARCHAR(160) NOT NULL DEFAULT 'system',
  seed_key VARCHAR(120) NULL UNIQUE,
  created_at DATETIME NOT NULL,
  UNIQUE KEY uq_review_run_fingerprint (experiment_id, variant_code, data_fingerprint),
  KEY ix_review_runs_verdict (verdict),
  KEY ix_review_runs_created_at (created_at),
  CONSTRAINT fk_review_run_experiment FOREIGN KEY (experiment_id) REFERENCES experiments(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS review_releases (
  id INT AUTO_INCREMENT PRIMARY KEY,
  experiment_id INT NOT NULL,
  run_id INT NOT NULL,
  variant_code VARCHAR(40) NOT NULL DEFAULT '',
  verdict VARCHAR(16) NOT NULL,
  snapshot JSON NULL,
  released_by VARCHAR(160) NOT NULL DEFAULT '',
  released_at DATETIME NOT NULL,
  seed_key VARCHAR(120) NULL UNIQUE,
  updated_at DATETIME NOT NULL,
  UNIQUE KEY uq_review_release_experiment (experiment_id),
  CONSTRAINT fk_review_release_experiment FOREIGN KEY (experiment_id) REFERENCES experiments(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
