import importlib
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_ROOT = REPO_ROOT / ".github" / "release"
sys.path.insert(0, str(RELEASE_ROOT))

release_core = importlib.import_module("ucm_release.core")
release_image = importlib.import_module("ucm_release.image")

INSTALL_COMMAND = "RUN pip install /workspace/package/uc_manager-*.whl"
POST_INSTALL_COMMAND = (
    "RUN if [ -f /workspace/package/install.sh ]; then \\\n"
    "        bash /workspace/package/install.sh; \\\n"
    "    fi"
)


class DockerWheelInstallTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = release_core.load_catalog()
        cls.recipes = {
            recipe["path"]: recipe for recipe in cls.catalog["docker_recipes"]
        }

    def _text(self, recipe):
        return (REPO_ROOT / recipe["path"]).read_text(encoding="utf-8")

    def _recipes_for(self, *, product=None, engine_type=None, upstream_variant=None):
        return [
            recipe
            for recipe in self.recipes.values()
            if (product is None or recipe["product"] == product)
            and (engine_type is None or recipe["engine_type"] == engine_type)
            and (
                upstream_variant is None
                or recipe.get("upstream_variant") == upstream_variant
            )
        ]

    def test_catalog_is_the_exact_repository_dockerfile_inventory(self):
        discovered = {
            path.relative_to(REPO_ROOT).as_posix()
            for path in (REPO_ROOT / "docker").glob("Dockerfile.ucm-*")
            if path.is_file()
        }

        self.assertEqual(set(self.recipes), discovered)
        self.assertEqual(len(self.recipes), len(self.catalog["docker_recipes"]))

    def test_declared_engine_and_install_hook_contracts_drive_each_recipe(self):
        for recipe in self.recipes.values():
            text = self._text(recipe)
            self.assertIn(f"ENV UCM_ENGINE_TYPE={recipe['engine_type']}", text)
            if recipe["install_hook"] == "required":
                self.assertIn(INSTALL_COMMAND, text, recipe["path"])
                self.assertIn(POST_INSTALL_COMMAND, text, recipe["path"])
                self.assertNotIn("uc_manager-*.whl &&", text, recipe["path"])
                self.assertNotIn("install_ucm_wheel.sh", text, recipe["path"])
            else:
                self.assertEqual(recipe["install_hook"], "none")
            if recipe["build_mode"] == "legacy-source-build":
                self.assertIn('ARG INSTALL_MODE="source"', text, recipe["path"])
            else:
                self.assertEqual(recipe["build_mode"], "generic-install-only")

    def test_declared_ascend_a3_recipes_build_the_a3_package(self):
        recipes = self._recipes_for(
            product="vllm-ascend",
            engine_type="vllm-ascend.a3",
            upstream_variant="a3",
        )
        self.assertTrue(recipes)
        for recipe in recipes:
            text = self._text(recipe)
            self.assertIn(
                "bash /workspace/unified-cache-management/scripts/build_ascend.sh -p ascend-a3",
                text,
                recipe["path"],
            )

    def test_declared_specialized_patches_live_in_selected_recipes(self):
        install_hook = (REPO_ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertNotIn('case "${UCM_ENGINE_TYPE:-}" in', install_hook)
        self.assertNotIn("boot_patch", install_hook)
        self.assertNotIn("sglang-adapt.patch", install_hook)

        mindie = self._recipes_for(product="mindie")
        sglang = self._recipes_for(product="sglang")
        self.assertTrue(mindie)
        self.assertTrue(sglang)
        for recipe in mindie:
            self.assertIn("Apply patch for MindIE", self._text(recipe))
            self.assertIn("boot_patch", self._text(recipe))
        for recipe in sglang:
            self.assertIn("Apply patch for SGLang", self._text(recipe))
            self.assertIn("sglang-adapt.patch", self._text(recipe))

    def test_repository_source_builds_have_no_formal_release_authority(self):
        for recipe in self.recipes.values():
            self.assertEqual(recipe["build_mode"], "legacy-source-build")
            self.assertNotIn("formal-release", recipe["lanes"])
            self.assertTrue(recipe["exclusion_reason"])

        self.assertEqual(release_image.DOCKER_ROOT, RELEASE_ROOT / "docker")
        self.assertTrue((release_image.DOCKER_ROOT / "Dockerfile").is_file())

    def test_build_scripts_package_install_hook(self):
        build_scripts = sorted((REPO_ROOT / "scripts").glob("build_*.sh"))

        self.assertEqual(len(build_scripts), 4)
        for path in build_scripts:
            text = path.read_text(encoding="utf-8")
            self.assertIn('cp "${KVCACHE_PROJECT_ROOT}/install.sh" .', text, path)
            self.assertIn("rm -f install.sh", text, path)

    def test_root_install_hook_documents_post_wheel_customization(self):
        installer = REPO_ROOT / "install.sh"
        text = installer.read_text(encoding="utf-8")

        self.assertIn("post-wheel", text)
        self.assertIn("custom installation steps", text)
        self.assertFalse((REPO_ROOT / "docker" / "install_ucm_wheel.sh").exists())


if __name__ == "__main__":
    unittest.main()
