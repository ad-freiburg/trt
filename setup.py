from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf8") as rdm:
    long_description = rdm.read()

with open("src/trt/version.py", "r", encoding="utf8") as vf:
    version = vf.readlines()[-1].strip().split()[-1].strip("\"'")

setup(
    name="trt",
    version=version,
    description="Tokenization repair using Transformers",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="Sebastian Walter",
    author_email="swalter@tf.uni-freiburg.de",
    python_requires=">=3.6",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    scripts=[
        "bin/trt"
    ],
    install_requires=[
        "torch>=1.8.0",
        "einops>=0.3.0",
        "numpy>=1.19.0",
        "tokenizers>=0.10.0",
        "pyyaml>=5.4.0",
        "tqdm>=4.49.0",
        "requests>=2.27.0",
        "flask>=2.0.0",
        "tabulate>=0.8.0"
    ],
    extras_require={
        "train": [
            "lmdb>=1.1.0",
            "msgpack>=1.0.0",
            "tensorboard>=2.8.0",
            "gputil>=1.4.0",
        ]
    }
)
