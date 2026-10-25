{
  description = "Viaduct: MCP server for verified copper work on a live KiCad board";

  # the rolling channel, so builds and tests run against the newest packages;
  # `nix flake update` moves the lock to its latest revision
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];

      # Viaduct runs on the newest releases. Where nixpkgs-unstable lags behind a pure-Python
      # dependency, it is built here with nixpkgs' own recipe at the newer version; bump the
      # version and hash to move on. Compiled packages (numpy, shapely, protobuf) follow
      # nixpkgs-unstable: rebuilding them would rebuild everything that depends on them.
      newer = {
        kicad-python = { version = "0.8.0"; hash = "sha256-peyyLJ+WJDiQz1o+NSgS4Cevc2tCHtNiQgkRMLguNRg="; };
        mcp = { version = "2.3.0"; hash = "sha256-CnXi78zYxm0/R4fON/DzRYMSDYM/XM/9SE3s+Cf1BR0="; };
      };

      pythonFor = pkgs: pkgs.python3.override {
        self = pythonFor pkgs;
        packageOverrides = final: prev: {
          # the recipe regenerates the protobuf code with nixpkgs' protoc, so kicad-python's
          # protobuf<6 pin does not apply here
          kicad-python = prev.kicad-python.overridePythonAttrs (old: {
            inherit (newer.kicad-python) version;
            src = pkgs.fetchFromGitLab {
              owner = "kicad/code";
              repo = "kicad-python";
              tag = newer.kicad-python.version;
              inherit (newer.kicad-python) hash;
              fetchSubmodules = true;
            };
            dependencies = old.dependencies ++ [ final.jsonschema ];
            pythonRelaxDeps = [ "protobuf" "pynng" ];
          });
          # mcp-types is built from the same source, so it follows
          mcp = prev.mcp.overridePythonAttrs (old: {
            inherit (newer.mcp) version;
            src = pkgs.fetchFromGitHub {
              owner = "modelcontextprotocol";
              repo = "python-sdk";
              tag = "v${newer.mcp.version}";
              inherit (newer.mcp) hash;
            };
            nativeCheckInputs = old.nativeCheckInputs ++ [ final.blockbuster ];   # new in 2.3
          });
        };
      };

      forAll = f: nixpkgs.lib.genAttrs systems (system:
        let pkgs = nixpkgs.legacyPackages.${system}; in f pkgs (pythonFor pkgs));
    in
    {
      packages = forAll (pkgs: python: {
        default = python.pkgs.buildPythonApplication {
          pname = "viaduct";
          version = "0.1.0";
          pyproject = true;
          src = ./.;
          build-system = [ python.pkgs.setuptools ];
          dependencies = with python.pkgs; [ kicad-python shapely numpy mcp ];
          nativeCheckInputs = [ python.pkgs.pytestCheckHook ];
          # open_headless needs xvfb-run; pcbnew comes from the user's KiCad on PATH
          makeWrapperArgs = [ "--suffix" "PATH" ":" "${pkgs.xvfb-run}/bin" ];
        };
      });

      # `nix flake check`: build the package, which runs the test suite
      checks = nixpkgs.lib.genAttrs systems (system: { default = self.packages.${system}.default; });

      devShells = forAll (pkgs: python: {
        default = pkgs.mkShell {
          packages = [
            (python.withPackages (p: with p; [ kicad-python shapely numpy mcp pytest ]))
          ];
        };
      });
    };
}
