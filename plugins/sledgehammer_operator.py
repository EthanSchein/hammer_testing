import os
import json
import subprocess
from airflow.models import BaseOperator
from airflow.utils.context import Context

class SledgehammerOperator(BaseOperator):
    def __init__(
        self,
        dag_id: str,
        base_cmd: str,
        user_workspace_root: str = "/home",
        bot_workspace_root: str = "/opt/sledgehammer/bot_workspace",
        allowed_bot_users: list[str] = None,
        *args, **kwargs
    ):
        super().__init__(*args, **kwargs)
        self.dag_id = dag_id
        self.base_cmd = base_cmd
        self.user_workspace_root = user_workspace_root
        self.bot_workspace_root = bot_workspace_root
        self.allowed_bot_users = allowed_bot_users or []

    def execute(self, context: Context):
        # The Airflow run context provides the user who triggered the DAG
        triggering_user = context['dag_run'].conf.get("triggering_user", "unknown_user")
        
        # Read the config injected by the FastAPI sidecar
        config_path = f"/opt/airflow/dags/{self.dag_id}_config.json"
        config = self._load_config(config_path)

        as_bot = config.get("as_bot", False)
        targets = config.get("targets", ["all"])
        compiler_flags = config.get("compiler_flags", "")

        if as_bot:
            if triggering_user not in self.allowed_bot_users:
                raise PermissionError(f"User {triggering_user} is not authorized to run as bot.")
            
            # Execute natively as sledgehammer-bot
            obj_dir = os.path.join(self.bot_workspace_root, self.dag_id)
            exec_cmd = f"{self.base_cmd} --obj_dir {obj_dir} {compiler_flags} {' '.join(targets)}"
        else:
            # Impersonate the triggering user
            obj_dir = os.path.join(self.user_workspace_root, triggering_user, "sledgehammer_builds", self.dag_id)
            # Wrap the Sledgehammer invocation in sudo
            exec_cmd = f"sudo -u {triggering_user} {self.base_cmd} --obj_dir {obj_dir} {compiler_flags} {' '.join(targets)}"

        self.log.info(f"Executing Sledgehammer: {exec_cmd}")
        
        result = subprocess.run(
            exec_cmd, 
            shell=True, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.STDOUT, 
            text=True
        )

        self.log.info(result.stdout)
        
        if result.returncode != 0:
            raise RuntimeError(f"Sledgehammer execution failed with return code {result.returncode}")

    def _load_config(self, path: str) -> dict:
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
        return {}