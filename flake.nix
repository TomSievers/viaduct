{
  description = "Viaduct: MCP server for verified copper work on a live KiCad board";

  inputs.nixpkgs.url = "nixpkgs";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAll (pkgs: {
        default = pkgs.python3Packages.buildPythonApplication {
          pname = "viaduct";
          version = "0.1.0";
          pyproject = true;
          src = ./.;
          build-system = [ pkgs.python3Packages.setuptools ];
          dependencies = with pkgs.python3Packages; [ kicad-python shapely numpy mcp ];
          nativeCheckInputs = [ pkgs.python3Packages.pytestCheckHook ];
          # open_headless needs xvfb-run; pcbnew comes from the user's KiCad on PATH
          makeWrapperArgs = [ "--suffix" "PATH" ":" "${pkgs.xvfb-run}/bin" ];
        };
      });

      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (p: with p; [ kicad-python shapely numpy mcp pytest ]))
          ];
        };
      });
    };
}
