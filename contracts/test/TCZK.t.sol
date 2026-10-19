// SPDX-License-Identifier: MIT
pragma solidity 0.8.31;

import {Test} from "forge-std/Test.sol";
import {IAccessControl} from "@openzeppelin/contracts/access/IAccessControl.sol";
import {IERC20Errors} from "@openzeppelin/contracts/interfaces/draft-IERC6093.sol";
import {ERC3009} from "@openzeppelin/contracts/token/ERC20/extensions/draft-ERC3009.sol";
import {TCZK} from "../src/TCZK.sol";

/// SPEC §3.6 cases. Signatures: vm.sign over the EIP-712 digest; `bytes` overload = abi.encodePacked(r, s, v).
contract TCZKTest is Test {
    bytes32 constant TRANSFER_TYPEHASH = keccak256(
        "TransferWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce)"
    );
    bytes32 constant RECEIVE_TYPEHASH = keccak256(
        "ReceiveWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce)"
    );
    bytes32 constant CANCEL_TYPEHASH = keccak256("CancelAuthorization(address authorizer,bytes32 nonce)");

    TCZK token;
    address admin = makeAddr("admin");
    address relayer = makeAddr("relayer"); // bank x402 relayer: ROUTER, not allowlisted
    address collector = makeAddr("collector"); // stands in for the escrow collector: ROUTER + allowlisted
    address alice;
    uint256 aliceKey;
    address bob = makeAddr("bob");
    address mallory = makeAddr("mallory"); // not a bank client

    function setUp() public {
        (alice, aliceKey) = makeAddrAndKey("alice");
        vm.warp(1_800_000_000);
        token = new TCZK(admin);
        vm.startPrank(admin);
        token.grantRole(token.ISSUER_ROLE(), admin);
        token.grantRole(token.COMPLIANCE_ROLE(), admin);
        token.grantRole(token.ENFORCER_ROLE(), admin);
        token.grantRole(token.ROUTER_ROLE(), relayer);
        token.grantRole(token.ROUTER_ROLE(), collector);
        token.allow(alice, keccak256("salt|1803729684/9999"));
        token.allow(bob, keccak256("salt|5638829718/9999"));
        token.allow(collector, bytes32(0));
        token.mint(alice, 100_000, "dep-1"); // 1000.00 Kč
        vm.stopPrank();
    }

    // ---- helpers ----

    function _digest(bytes32 typehash, address from, address to, uint256 value, uint256 va, uint256 vb, bytes32 nonce)
        internal
        view
        returns (bytes32)
    {
        bytes32 structHash = keccak256(abi.encode(typehash, from, to, value, va, vb, nonce));
        return keccak256(abi.encodePacked("\x19\x01", token.DOMAIN_SEPARATOR(), structHash));
    }

    function _sign(uint256 key, bytes32 digest) internal pure returns (uint8 v, bytes32 r, bytes32 s) {
        (v, r, s) = vm.sign(key, digest);
    }

    function _sigBytes(uint256 key, bytes32 digest) internal pure returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(key, digest);
        return abi.encodePacked(r, s, v);
    }

    function _transferSig(address to, uint256 value, uint256 vb, bytes32 nonce) internal view returns (bytes memory) {
        return _sigBytes(aliceKey, _digest(TRANSFER_TYPEHASH, alice, to, value, 0, vb, nonce));
    }

    // ---- metadata ----

    function test_metadata() public view {
        assertEq(token.name(), "tCZK Deposit Token");
        assertEq(token.symbol(), "tCZK");
        assertEq(token.decimals(), 2);
        assertEq(token.version(), "1");
        (, string memory name, string memory version, uint256 chainId, address verifying,,) = token.eip712Domain();
        assertEq(name, token.name()); // x402 needs ERC-20 name == EIP-712 name
        assertEq(version, "1");
        bytes32 expected = keccak256(
            abi.encode(
                keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"),
                keccak256(bytes(name)),
                keccak256(bytes(version)),
                chainId,
                verifying
            )
        );
        assertEq(token.DOMAIN_SEPARATOR(), expected);
        assertEq(token.accountRefOf(alice), keccak256("salt|1803729684/9999"));
        assertTrue(token.isAllowed(alice));
        assertTrue(token.strictMode());
    }

    // ---- EIP-3009: both overloads ----

    function test_transferWithAuthorization_vrs() public {
        bytes32 nonce = keccak256("n1");
        (uint8 v, bytes32 r, bytes32 s) =
            _sign(aliceKey, _digest(TRANSFER_TYPEHASH, alice, bob, 12_500, 0, block.timestamp + 60, nonce));
        vm.prank(relayer);
        token.transferWithAuthorization(alice, bob, 12_500, 0, block.timestamp + 60, nonce, v, r, s);
        assertEq(token.balanceOf(bob), 12_500);
        assertTrue(token.authorizationState(alice, nonce));
    }

    function test_transferWithAuthorization_bytes() public {
        bytes32 nonce = keccak256("n2");
        bytes memory sig = _transferSig(bob, 12_500, block.timestamp + 60, nonce);
        vm.prank(relayer);
        token.transferWithAuthorization(alice, bob, 12_500, 0, block.timestamp + 60, nonce, sig);
        assertEq(token.balanceOf(bob), 12_500);
        assertEq(token.balanceOf(alice), 100_000 - 12_500);
    }

    function test_receiveWithAuthorization_vrs() public {
        bytes32 nonce = keccak256("r1");
        (uint8 v, bytes32 r, bytes32 s) =
            _sign(aliceKey, _digest(RECEIVE_TYPEHASH, alice, collector, 500, 0, block.timestamp + 60, nonce));
        vm.prank(collector);
        token.receiveWithAuthorization(alice, collector, 500, 0, block.timestamp + 60, nonce, v, r, s);
        assertEq(token.balanceOf(collector), 500);
    }

    function test_receiveWithAuthorization_bytes_selector() public {
        bytes32 nonce = keccak256("r2");
        bytes memory sig = _sigBytes(aliceKey, _digest(RECEIVE_TYPEHASH, alice, collector, 500, 0, block.timestamp + 60, nonce));
        // The escrow collector calls exactly this selector.
        bytes memory data = abi.encodeWithSelector(
            bytes4(0x88b7ab63), alice, collector, uint256(500), uint256(0), block.timestamp + 60, nonce, sig
        );
        vm.prank(collector);
        (bool ok,) = address(token).call(data);
        assertTrue(ok);
        assertEq(token.balanceOf(collector), 500);
    }

    // ---- EIP-3009 failures ----

    function test_replay_reverts() public {
        bytes32 nonce = keccak256("n3");
        bytes memory sig = _transferSig(bob, 100, block.timestamp + 60, nonce);
        vm.startPrank(relayer);
        token.transferWithAuthorization(alice, bob, 100, 0, block.timestamp + 60, nonce, sig);
        vm.expectRevert(abi.encodeWithSelector(ERC3009.ERC3009UsedAuthorization.selector, alice, nonce));
        token.transferWithAuthorization(alice, bob, 100, 0, block.timestamp + 60, nonce, sig);
        vm.stopPrank();
    }

    function test_expired_reverts() public {
        bytes32 nonce = keccak256("n4");
        uint256 vb = block.timestamp + 60;
        bytes memory sig = _transferSig(bob, 100, vb, nonce);
        vm.warp(vb);
        vm.expectRevert(abi.encodeWithSelector(ERC3009.ERC3009InvalidAuthorizationTime.selector, 0, vb));
        vm.prank(relayer);
        token.transferWithAuthorization(alice, bob, 100, 0, vb, nonce, sig);
    }

    function test_wrong_signer_reverts() public {
        (, uint256 otherKey) = makeAddrAndKey("other");
        bytes32 nonce = keccak256("n5");
        bytes memory sig = _sigBytes(otherKey, _digest(TRANSFER_TYPEHASH, alice, bob, 100, 0, block.timestamp + 60, nonce));
        vm.expectRevert(ERC3009.ERC3009InvalidSignature.selector);
        vm.prank(relayer);
        token.transferWithAuthorization(alice, bob, 100, 0, block.timestamp + 60, nonce, sig);
    }

    function test_tampered_amount_reverts() public {
        bytes32 nonce = keccak256("n6");
        bytes memory sig = _transferSig(bob, 100, block.timestamp + 60, nonce);
        vm.expectRevert(ERC3009.ERC3009InvalidSignature.selector);
        vm.prank(relayer);
        token.transferWithAuthorization(alice, bob, 10_000, 0, block.timestamp + 60, nonce, sig);
    }

    function test_receive_not_from_to_reverts() public {
        bytes32 nonce = keccak256("r3");
        bytes memory sig = _sigBytes(aliceKey, _digest(RECEIVE_TYPEHASH, alice, collector, 500, 0, block.timestamp + 60, nonce));
        vm.expectRevert(abi.encodeWithSelector(IERC20Errors.ERC20InvalidReceiver.selector, collector));
        vm.prank(relayer); // a front-runner submitting the collector's authorization
        token.receiveWithAuthorization(alice, collector, 500, 0, block.timestamp + 60, nonce, sig);
    }

    function test_cancel_then_reuse_nonce_reverts() public {
        bytes32 nonce = keccak256("c1");
        bytes memory cancelSig = _sigBytes(
            aliceKey,
            keccak256(abi.encodePacked("\x19\x01", token.DOMAIN_SEPARATOR(), keccak256(abi.encode(CANCEL_TYPEHASH, alice, nonce))))
        );
        token.cancelAuthorization(alice, nonce, cancelSig);
        assertTrue(token.authorizationState(alice, nonce));
        bytes memory sig = _transferSig(bob, 100, block.timestamp + 60, nonce);
        vm.expectRevert(abi.encodeWithSelector(ERC3009.ERC3009UsedAuthorization.selector, alice, nonce));
        vm.prank(relayer);
        token.transferWithAuthorization(alice, bob, 100, 0, block.timestamp + 60, nonce, sig);
    }

    function test_cancel_vrs() public {
        bytes32 nonce = keccak256("c2");
        bytes32 digest =
            keccak256(abi.encodePacked("\x19\x01", token.DOMAIN_SEPARATOR(), keccak256(abi.encode(CANCEL_TYPEHASH, alice, nonce))));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(aliceKey, digest);
        token.cancelAuthorization(alice, nonce, v, r, s);
        assertTrue(token.authorizationState(alice, nonce));
    }

    // ---- allowlist / freeze ----

    function test_to_not_allowlisted_reverts() public {
        bytes32 nonce = keccak256("a1");
        bytes memory sig = _transferSig(mallory, 100, block.timestamp + 60, nonce);
        vm.expectRevert(abi.encodeWithSelector(TCZK.NotAllowlisted.selector, mallory));
        vm.prank(relayer);
        token.transferWithAuthorization(alice, mallory, 100, 0, block.timestamp + 60, nonce, sig);
    }

    function test_from_delisted_reverts() public {
        vm.prank(admin);
        token.disallow(alice);
        assertEq(token.accountRefOf(alice), bytes32(0));
        bytes32 nonce = keccak256("a2");
        bytes memory sig = _transferSig(bob, 100, block.timestamp + 60, nonce);
        vm.expectRevert(abi.encodeWithSelector(TCZK.NotAllowlisted.selector, alice));
        vm.prank(relayer);
        token.transferWithAuthorization(alice, bob, 100, 0, block.timestamp + 60, nonce, sig);
    }

    function test_frozen_sender_and_receiver_revert() public {
        vm.prank(admin);
        token.freeze(alice);
        bytes32 nonce = keccak256("f1");
        bytes memory sig = _transferSig(bob, 100, block.timestamp + 60, nonce);
        vm.expectRevert(abi.encodeWithSelector(TCZK.AccountFrozen.selector, alice));
        vm.prank(relayer);
        token.transferWithAuthorization(alice, bob, 100, 0, block.timestamp + 60, nonce, sig);

        vm.startPrank(admin);
        token.unfreeze(alice);
        token.freeze(bob);
        vm.stopPrank();
        vm.expectRevert(abi.encodeWithSelector(TCZK.AccountFrozen.selector, bob));
        vm.prank(relayer);
        token.transferWithAuthorization(alice, bob, 100, 0, block.timestamp + 60, nonce, sig);

        vm.prank(admin);
        token.unfreeze(bob);
        vm.prank(relayer);
        token.transferWithAuthorization(alice, bob, 100, 0, block.timestamp + 60, nonce, sig);
        assertEq(token.balanceOf(bob), 100);
    }

    function test_mint_to_non_client_reverts() public {
        vm.expectRevert(abi.encodeWithSelector(TCZK.NotAllowlisted.selector, mallory));
        vm.prank(admin);
        token.mint(mallory, 100, "dep-2");
    }

    function test_only_roles_can_admin() public {
        bytes32 issuer = token.ISSUER_ROLE();
        bytes32 compliance = token.COMPLIANCE_ROLE();
        vm.startPrank(mallory);
        vm.expectRevert(abi.encodeWithSelector(IAccessControl.AccessControlUnauthorizedAccount.selector, mallory, issuer));
        token.mint(mallory, 100, "x");
        vm.expectRevert(
            abi.encodeWithSelector(IAccessControl.AccessControlUnauthorizedAccount.selector, mallory, compliance)
        );
        token.allow(mallory, bytes32(0));
        vm.expectRevert(
            abi.encodeWithSelector(IAccessControl.AccessControlUnauthorizedAccount.selector, mallory, compliance)
        );
        token.freeze(alice);
        vm.stopPrank();
    }

    // ---- burn / forcedTransfer ----

    function test_burn_of_delisted_client() public {
        vm.startPrank(admin);
        token.disallow(alice);
        token.freeze(alice);
        token.burn(alice, 100_000, "wd-1");
        vm.stopPrank();
        assertEq(token.balanceOf(alice), 0);
        assertEq(token.totalSupply(), 0);
    }

    function test_forcedTransfer_from_frozen() public {
        vm.startPrank(admin);
        token.freeze(alice);
        vm.expectEmit(address(token));
        emit TCZK.ForcedTransfer(alice, bob, 40_000, "case-7");
        token.forcedTransfer(alice, bob, 40_000, "case-7");
        vm.stopPrank();
        assertEq(token.balanceOf(bob), 40_000);
    }

    function test_forcedTransfer_rules() public {
        vm.prank(admin);
        vm.expectRevert(abi.encodeWithSelector(TCZK.NotAllowlisted.selector, mallory));
        token.forcedTransfer(alice, mallory, 1, "case-8");

        bytes32 enforcer = token.ENFORCER_ROLE();
        vm.prank(relayer);
        vm.expectRevert(abi.encodeWithSelector(IAccessControl.AccessControlUnauthorizedAccount.selector, relayer, enforcer));
        token.forcedTransfer(alice, bob, 1, "case-9");
    }

    // ---- strict mode (§3.4) ----

    function test_strict_direct_transfer_reverts() public {
        vm.expectRevert(abi.encodeWithSelector(TCZK.TransferViaBankOnly.selector, alice));
        vm.prank(alice);
        token.transfer(bob, 100);
    }

    function test_strict_transferFrom_by_non_router_reverts() public {
        vm.prank(alice);
        token.approve(bob, 100);
        vm.expectRevert(abi.encodeWithSelector(TCZK.TransferViaBankOnly.selector, bob));
        vm.prank(bob);
        token.transferFrom(alice, bob, 100);
    }

    function test_strict_signed_transfer_by_non_router_reverts() public {
        bytes32 nonce = keccak256("s1");
        bytes memory sig = _transferSig(bob, 100, block.timestamp + 60, nonce);
        vm.expectRevert(abi.encodeWithSelector(TCZK.TransferViaBankOnly.selector, mallory));
        vm.prank(mallory); // anyone could submit a signed authorization; only the bank's relayer may
        token.transferWithAuthorization(alice, bob, 100, 0, block.timestamp + 60, nonce, sig);
    }

    function test_strict_off_allows_direct_transfer() public {
        vm.prank(admin);
        token.setStrictMode(false);
        vm.prank(alice);
        token.transfer(bob, 100);
        assertEq(token.balanceOf(bob), 100);
        vm.expectRevert(abi.encodeWithSelector(TCZK.NotAllowlisted.selector, mallory));
        vm.prank(alice);
        token.transfer(mallory, 100); // allowlist still applies
    }
}
