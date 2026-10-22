# Regenerate the Grafana dashboard JSON from the Python generators.
#
#   make dashboards          build for your inventory (pcp/openmetrics/sites.json,
#                            falling back to sites.example.json) into build/
#   make example-dashboards  rebuild the committed grafana/dashboards/*.json from
#                            sites.example.json
DASHBOARDS := hypervisor-vm-overview:gen_dashboard fleet-health:gen_fleet_dashboard ops-status:gen_status_dashboard

.PHONY: dashboards example-dashboards clean
dashboards:
	@mkdir -p build
	@for d in $(DASHBOARDS); do python3 grafana/$${d##*:}.py > build/$${d%%:*}.json && echo "build/$${d%%:*}.json"; done

example-dashboards:
	@for d in $(DASHBOARDS); do PCP_SITES=pcp/openmetrics/sites.example.json python3 grafana/$${d##*:}.py > grafana/dashboards/$${d%%:*}.json && echo "grafana/dashboards/$${d%%:*}.json"; done

clean:
	rm -rf build grafana/__pycache__
