.PHONY: eval-check wecom-test scheduler crash-test mysql-up mysql-down mysql-cli install docker langfuse-init langfuse-up langfuse-down langfuse-logs langfuse-ps langfuse-password init-prompts dev experiment consult-test consult-smoke rural-test rural-smoke

PY ?= .venv/bin/python
LF  = cd deploy/langfuse && docker compose

install:            ## 创建虚拟环境并安装依赖
	python3 -m venv .venv && .venv/bin/pip install -U pip && .venv/bin/pip install -r requirements.txt
	@test -f .env || cp .env.example .env

docker:             ## 启动 Docker 运行时（Colima，无需 Docker Desktop）
	colima status >/dev/null 2>&1 || colima start
	docker context use colima

langfuse-init: docker  ## 首次：生成密钥 + 启动 Langfuse + 把 Key 写入 .env
	bash scripts/langfuse_init.sh

langfuse-up: docker    ## 启动 Langfuse（Postgres / ClickHouse / Redis / MinIO / web / worker）
	$(LF) up -d
	@until curl -sf --noproxy '*' http://localhost:3000/api/public/health >/dev/null; do sleep 3; done; echo "Langfuse 已就绪: http://localhost:3000"

langfuse-down:      ## 停止 Langfuse（保留数据）
	$(LF) down

langfuse-logs:
	$(LF) logs -f --tail 100 langfuse-web langfuse-worker

langfuse-ps:
	$(LF) ps

langfuse-password:  ## 查看 Langfuse 登录账号密码
	@grep -E '^LANGFUSE_INIT_USER_(EMAIL|PASSWORD)=' deploy/langfuse/.env

mysql-up: docker     ## 启动 MySQL（企微群回复业务存储，127.0.0.1:13306）
	cd deploy/mysql && docker compose up -d
	@until docker exec lchain-mysql mysqladmin ping -h127.0.0.1 -uroot -plchain_root --silent 2>/dev/null; do sleep 2; done; echo "MySQL 已就绪: 127.0.0.1:13306 (lchain/lchain)"

mysql-down:         ## 停止 MySQL（保留数据）
	cd deploy/mysql && docker compose down

mysql-cli:          ## 进入 MySQL 命令行
	docker exec -it lchain-mysql mysql -ulchain -plchain lchain

init-prompts:       ## 把默认提示词同步到 Langfuse
	$(PY) -m scripts.init_prompts

scheduler:          ## 启动定时任务进程（只部署一个；超时通知 / 补投递 / 接管失联节点 / 清理 checkpoint）
	$(PY) -m app.scheduler

dev:                ## 启动 Web 服务
	$(PY) run.py

crash-test:         ## 企微群回复重启续跑测试（真实杀进程，需先停掉 make dev）
	$(PY) scripts/crash_test.py && BURST=1 $(PY) scripts/crash_test.py && WHDOWN=1 $(PY) scripts/crash_test.py

experiment:         ## 用数据集跑一次提示词评估
	$(PY) -m scripts.run_experiment

eval-check:          ## 校验 eval/ 下的用例文件格式（问诊 + 山区问诊 + 企微，有错时退出码 1；新增/改用例后先跑这个）
	PYTHONPATH=. $(PY) -m app.consult.cases
	PYTHONPATH=. $(PY) -m app.rural.cases
	PYTHONPATH=. $(PY) -m app.wecom.cases

wecom-test:         ## 企微群回复自动化测试（评测判定 / 用例加载 / 打卡刷新恢复，不起模型）
	PYTHONPATH=. $(PY) -m pytest tests/agent/test_wecom_eval.py -q

consult-test:       ## 问诊工作流自动化测试（10 个常见病例 + 钳制/截断/安全边界，不起模型）
	PYTHONPATH=. $(PY) -m pytest tests/agent/test_consult.py -q

consult-smoke:      ## 问诊工作流真实模型端到端冒烟（消耗 token；CASE=3 指定病例，ALL=1 跑全部）
	PYTHONPATH=. $(PY) -m scripts.consult_smoke $(if $(ALL),--all,) $(if $(CASE),--case $(CASE),)

rural-test:         ## 山区问诊工作流自动化测试（11 个山区用例 + 就医条件门禁/转诊决策/无剂量，不起模型）
	PYTHONPATH=. $(PY) -m pytest tests/agent/test_rural.py -q

rural-smoke:        ## 山区问诊真实模型端到端冒烟（消耗 token；CASE=3 指定用例，ALL=1 跑全部）
	PYTHONPATH=. $(PY) -m scripts.rural_smoke $(if $(ALL),--all,) $(if $(CASE),--case $(CASE),)
