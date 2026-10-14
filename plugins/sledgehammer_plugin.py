import requests
from airflow.plugins_manager import AirflowPlugin
from flask_appbuilder import expose, BaseView
from flask import request, flash, redirect, render_template_string

# Internal API Sidecar endpoint
SIDECAR_URL = "http://127.0.0.1:8000/api/v1/dags"

HTML_TEMPLATE = """
{% extends "airflow/main.html" %}
{% block content %}
  <h2>Sledgehammer: Register or Modify SoC DAG</h2>
  <hr>
  <form method="POST" action="/sledgehammer/submit">
    <div class="form-group">
      <label>DAG ID</label>
      <input type="text" name="dag_id" class="form-control" placeholder="e.g., alice_my_soc_build" required>
    </div>
    <div class="form-group">
      <label>Hammer Project Config (YAML/JSON absolute path)</label>
      <input type="text" name="project_config" class="form-control" placeholder="/home/user/repo/soc_config.yaml" required>
    </div>
    <div class="form-group">
      <label>Default Targets (comma separated)</label>
      <input type="text" name="targets" class="form-control" value="synth, par">
    </div>
    <button type="submit" class="btn btn-primary">Submit DAG</button>
  </form>
{% endblock %}
"""

class SledgehammerView(BaseView):
    default_view = 'manage'
    route_base = '/sledgehammer'

    @expose('/manage')
    def manage(self):
        # Render the form directly
        return render_template_string(HTML_TEMPLATE)

    @expose('/submit', methods=['POST'])
    def submit(self):
        dag_id = request.form.get('dag_id')
        project_config = request.form.get('project_config')
        targets = [t.strip() for t in request.form.get('targets', 'synth, par').split(',')]

        payload = {
            "dag_id": dag_id,
            "project_configs": [project_config],
            "default_targets": targets
        }

        try:
            # Forward the request to the FastAPI sidecar running as sledgehammer-bot
            # (You will need to pass the JWT token here if your sidecar requires it)
            response = requests.post(f"{SIDECAR_URL}/create", json=payload, timeout=5)
            if response.status_code == 200:
                flash(f"DAG '{dag_id}' registered successfully!", "success")
            elif response.status_code == 409:
                # Implement modify logic if DAG exists
                response = requests.put(f"{SIDECAR_URL}/{dag_id}/modify", json=payload, timeout=5)
                flash(f"DAG '{dag_id}' updated successfully!", "success")
            else:
                flash(f"Error registering DAG: {response.text}", "error")
        except Exception as e:
            flash(f"Failed to communicate with bot sidecar: {str(e)}", "error")

        return redirect('/sledgehammer/manage')

v_appbuilder_view = SledgehammerView()
v_appbuilder_package = {
    "name": "Manage SoC DAGs",
    "category": "Sledgehammer",
    "view": v_appbuilder_view
}

class SledgehammerUIPlugin(AirflowPlugin):
    name = "sledgehammer_ui"
    appbuilder_views = [v_appbuilder_package]