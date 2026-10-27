{
  description = "KiCad built from the newest upstream sources, to check whether a KiCad bug is already fixed";

  # Two branches, because a fix lands on master first and is backported to the release branch:
  # - master: KiCad's development branch (the next major release)
  # - 10.0: the release branch the next 10.0.x is cut from
  # `nix flake update` in this directory moves both to their current head.
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    kicad-master = {
      url = "git+https://gitlab.com/kicad/code/kicad.git?ref=master&shallow=1";
      flake = false;
    };
    kicad-10 = {
      url = "git+https://gitlab.com/kicad/code/kicad.git?ref=10.0&shallow=1";
      flake = false;
    };
  };

  outputs = { self, nixpkgs, kicad-master, kicad-10 }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});

      # nixpkgs' kicad-unstable recipe with the source swapped for a flake input. Its `srcs`
      # override only applies to the unstable recipe, so the 10.0 branch is built through it
      # too; the recipe stamps the version from the source's commit either way. The symbol
      # and footprint libraries are the ones nixpkgs pins for kicad-unstable.
      # The recipe stamps the commit into the version by replacing "unknown" in
      # KiCadVersion.cmake. Untagged commits carry it ("10.99.0-unknown"), but a release
      # candidate on the 10.0 branch does not ("10.0.8-rc1"), so add it where it is missing.
      kicadSource = pkgs: branch: src:
        pkgs.runCommand "kicad-${branch}-source" { inherit (src) rev shortRev; } ''
          cp -r ${src} $out
          chmod -R u+w $out
          v=$out/cmake/KiCadVersion.cmake
          grep -q 'KICAD_SEMANTIC_VERSION "[^"]*unknown"' $v ||
            sed -i 's/\(KICAD_SEMANTIC_VERSION "[^"]*\)"/\1-unknown"/' $v
        '';

      # nixpkgs' recipe is written for the commit it pins, and a branch moves on: a patch stops
      # applying, a build tool becomes required. The recipe builds KiCad itself in base.nix
      # through its `callPackage` argument, so that is where a branch's adjustments go:
      # `patches` maps a patch's file name to a replacement, and the input lists are added.
      kicadFrom = pkgs: branch: src: { patches ? { }, nativeBuildInputs ? [ ], buildInputs ? [ ] }:
        (pkgs.kicad-unstable.override {
          pname = "kicad-${branch}";
          with3d = false; # the 3D models are gigabytes and no API bug needs them
          callPackage = path: args:
            let drv = pkgs.callPackage path args; in
            if baseNameOf (toString path) == "base.nix" then
              drv.overrideAttrs (old: {
                patches = map (p: patches.${baseNameOf (toString p)} or p) (old.patches or [ ]);
                nativeBuildInputs = (old.nativeBuildInputs or [ ]) ++ nativeBuildInputs;
                buildInputs = (old.buildInputs or [ ]) ++ buildInputs;
              })
            else drv;
          srcs = {
            kicad = kicadSource pkgs branch src;
            kicadVersion = "${branch}-${src.shortRev}";
          };
        }).overrideAttrs (old: {
          passthru = (old.passthru or { }) // {
            upstream = {
              inherit branch;
              inherit (src) rev lastModifiedDate;
            };
          };
        });
    in
    {
      # not `rec`: inside it kicad-master would name the package, not the source input
      packages = forAll (pkgs:
        let
          master = kicadFrom pkgs "master" kicad-master {
            # master reworked PATHS::GetStockDataPath; the same override, at its new place
            patches."runtime_stock_data_path.patch" = ./runtime_stock_data_path-master.patch;
            # master's rendering layer (common/rhi) compiles its Vulkan shaders at build time
            nativeBuildInputs = map pkgs.lib.getBin [ pkgs.glslang pkgs.spirv-tools ];
            buildInputs = [ pkgs.vulkan-headers ];
          };
        in {
          kicad-master = master;
          kicad-10 = kicadFrom pkgs "10.0" kicad-10 { };
          default = master;
        });

      # `nix run .#revisions`: which upstream commits this lock builds
      apps = forAll (pkgs: {
        revisions = {
          type = "app";
          program = toString (pkgs.writeShellScript "kicad-upstream-revisions" ''
            echo "master ${kicad-master.rev} (${kicad-master.lastModifiedDate})"
            echo "10.0   ${kicad-10.rev} (${kicad-10.lastModifiedDate})"
          '');
        };
      });
    };
}
