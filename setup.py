from setuptools import find_packages, setup

setup(
    name="skills-workspace",
    version="0.2.0",
    description="轨道车辆实训缺陷闭环服务",
    package_dir={"": "src"},
    packages=find_packages("src"),
    python_requires=">=3.11",
)
