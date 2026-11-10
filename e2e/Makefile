vlsi_dir=$(abspath .)



# minimal flow configuration variables
design              ?= gcd
pdk                 ?= sky130
tools               ?= cm
env                 ?= bwrc

extra               ?= #"configs-design/$(design)/mock_hier.yml"  # extra configs
args                ?=  # command-line args (including step flow control)

HAMMER_DRIVER		?= ./example-driver
OBJ_DIR             ?= $(vlsi_dir)/build-$(pdk)-$(tools)/$(design)

# non-overlapping default configs
ENV_YML             ?= configs-env/$(env)-env.yml
PDK_CONF            ?= configs-pdk/$(pdk).yml
TOOLS_CONF          ?= configs-tool/$(tools).yml

# design-specific overrides of default configs
DESIGN_CONF         ?= configs-design/$(design)/common.yml
DESIGN_PDK_CONF     ?= configs-design/$(design)/$(pdk).yml
SIM_CONF            ?= $(if $(findstring -rtl,$(MAKECMDGOALS)), configs-design/$(design)/sim-rtl.yml, \
                       $(if $(findstring -syn,$(MAKECMDGOALS)), configs-design/$(design)/sim-syn.yml, \
                       $(if $(findstring -par,$(MAKECMDGOALS)), configs-design/$(design)/sim-par.yml, )))
POWER_CONF          ?= $(if $(findstring power-rtl,$(MAKECMDGOALS)), configs-design/$(design)/power-rtl-$(pdk).yml, \
                       $(if $(findstring power-syn,$(MAKECMDGOALS)), configs-design/$(design)/power-syn-$(pdk).yml, \
                       $(if $(findstring power-par,$(MAKECMDGOALS)), configs-design/$(design)/power-par-$(pdk).yml, )))
# names the corrected netlist the eco stage patches toward; without it par-to-eco says what is missing
ECO_CONF            ?= $(if $(findstring eco,$(MAKECMDGOALS)), $(wildcard configs-design/$(design)/eco.yml), )
SRAM_CONF           ?= $(OBJ_DIR)/sram_generator-output.json

# A project from `studio register-projects` (registration.json) gets its hammer.d from `studio make-dag`,
# which bakes in the DAG's configs, its SRAMs ($(pdk)-extras.yml) included. Passing its configs again, or
# sram_generator's output, would change the config the DAG ran syn and par with, and rerun both.
REGISTERED          := $(wildcard configs-design/$(design)/registration.json)

PROJ_YMLS           ?= $(if $(REGISTERED),$(extra),$(PDK_CONF) $(TOOLS_CONF) $(DESIGN_CONF) $(DESIGN_PDK_CONF) $(SIM_CONF) $(POWER_CONF) $(ECO_CONF) $(extra))
HAMMER_EXTRA_ARGS   ?= $(foreach conf, $(PROJ_YMLS), -p $(conf)) $(if $(REGISTERED),,-p $(SRAM_CONF)) $(args)




HAMMER_D_MK = $(OBJ_DIR)/hammer.d

build: $(HAMMER_D_MK)

ifneq ($(REGISTERED),)
$(HAMMER_D_MK):
	$(STUDIO) make-dag --design $(design) --pdk $(pdk) --tools $(tools) --env $(env) --obj-dir $(OBJ_DIR)
else
$(HAMMER_D_MK): $(SRAM_CONF)
	$(HAMMER_DRIVER) --obj_dir $(OBJ_DIR) -e $(ENV_YML) $(HAMMER_EXTRA_ARGS) build
endif

# designs works on every project, not $(design): don't build that design's hammer.d just to include it
ifeq ($(filter designs,$(MAKECMDGOALS)),)
-include $(HAMMER_D_MK)
endif

# Register each project under ../src/original as configs-design/<project>/ and link its DAG
# (project=<name> for just one). See readme_documents/EECS151_PROJECTS.md.
# the checkout's own venv when there is one, so this works without activating it
STUDIO              ?= $(firstword $(wildcard $(vlsi_dir)/../.venv/bin/studio) studio)
designs:
	$(STUDIO) register-projects --env $(env) --pdk $(pdk) --tools $(tools) $(if $(project),--project $(project))
.PHONY: designs

$(SRAM_CONF) srams:
	$(HAMMER_DRIVER) --obj_dir $(OBJ_DIR) -e $(ENV_YML) $(foreach conf, $(PROJ_YMLS), -p $(conf)) sram_generator
	cp output.json $(SRAM_CONF)

clean:
	rm -rf $(OBJ_DIR) hammer-vlsi-*.log
