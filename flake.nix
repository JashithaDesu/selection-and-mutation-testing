{
  description = "selection-and-mutation-testing: reproducible environment";

  # One nixpkgs commit pins every package. The exact commit is recorded in
  # flake.lock: commit that file, or nothing is really pinned.
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.05";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      python = pkgs.python312;
    in {
      # nix develop  ->  a shell with exactly this Python
      devShells.${system}.default = pkgs.mkShell {
        packages = [ python pkgs.git ];
      };

      # nix build .#experiment  ->  runs the full mutation experiment inside
      # the Nix sandbox (no network, no access to /home, fixed environment
      # variables) and outputs only the functional fingerprint.
      # Timings are left out on purpose: they differ on every run, and
      # `nix build --rebuild` requires byte-identical output.
      # The e2e tests start a server on 127.0.0.1; the Linux sandbox
      # provides a loopback interface, so they still run.
      packages.${system}.experiment = pkgs.stdenvNoCC.mkDerivation {
        name = "smt-experiment";
        src = self;
        nativeBuildInputs = [ python ];
        buildPhase = ''
          export HOME=$TMPDIR
          export PYTHONDONTWRITEBYTECODE=1
          python -m testchain all --jobs ''${NIX_BUILD_CORES:-2}
          python env/fingerprint.py results --functional-only > fingerprint.json
          cat fingerprint.json
        '';
        installPhase = ''
          mkdir -p $out
          cp fingerprint.json $out/
        '';
      };
    };
}
