// SPDX-License-Identifier: MIT
pragma solidity 0.8.31;

import {Test} from "forge-std/Test.sol";
import {TCZK} from "../src/TCZK.sol";

/// AuthCaptureEscrow v1.1 on Base Sepolia (base/commerce-payments b748fdd), field order = hashing order (§5.1).
struct PaymentInfo {
    address operator;
    address payer;
    address receiver;
    address token;
    uint120 maxAmount;
    uint48 preApprovalExpiry;
    uint48 authorizationExpiry;
    uint48 refundExpiry;
    uint16 minFeeBps;
    uint16 maxFeeBps;
    address feeReceiver;
    uint256 salt;
}

interface IAuthCaptureEscrow {
    function getHash(PaymentInfo calldata paymentInfo) external view returns (bytes32);
    function getTokenStore(address operator) external view returns (address);
    function paymentState(bytes32 paymentInfoHash)
        external
        view
        returns (bool hasCollectedPayment, uint120 capturableAmount, uint120 refundableAmount);
    function authorize(PaymentInfo calldata paymentInfo, uint256 amount, address tokenCollector, bytes calldata collectorData)
        external;
    function capture(PaymentInfo calldata paymentInfo, uint256 amount, uint256 feeAmount, address feeReceiver) external;
    function void(PaymentInfo calldata paymentInfo) external;
}

/// Fork test of the tCZK escrow cycle through the real collector and escrow (SPEC §3.6, §5.4, §5.6):
/// strict mode on, collector + getTokenStore(operator) allowlisted and ROUTER before the first authorize.
/// Run: forge test --match-contract TCZKEscrowFork (needs network; FORK_URL overrides the public RPC).
contract TCZKEscrowForkTest is Test {
    IAuthCaptureEscrow constant ESCROW = IAuthCaptureEscrow(0xf96815976523E00e65Be8f34cA5e64b4f41EB19c);
    address constant COLLECTOR = 0x8612dfdc421f80336cd14E8EF9cb1E765dB5ab88;
    bytes32 constant RECEIVE_TYPEHASH = keccak256(
        "ReceiveWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce)"
    );

    TCZK token;
    address admin = makeAddr("bank-admin");
    address operator = makeAddr("bank-operator"); // fresh operator: its token store does not exist yet
    address seller = makeAddr("beta");
    address buyer;
    uint256 buyerKey;
    address store;

    function setUp() public {
        vm.createSelectFork(vm.envOr("FORK_URL", string("https://sepolia.base.org")), 48_319_000);
        (buyer, buyerKey) = makeAddrAndKey("alfa");
        token = new TCZK(admin);
        store = ESCROW.getTokenStore(operator);
        assertEq(store.code.length, 0, "token store is created lazily, on the first capture/void");
        vm.startPrank(admin);
        token.grantRole(token.ISSUER_ROLE(), admin);
        token.grantRole(token.COMPLIANCE_ROLE(), admin);
        token.allow(buyer, keccak256("salt|1803729684/9999"));
        token.allow(seller, keccak256("salt|5638829718/9999"));
        token.allow(COLLECTOR, bytes32(0));
        token.allow(store, bytes32(0));
        token.grantRole(token.ROUTER_ROLE(), COLLECTOR);
        token.mint(buyer, 100_000, "dep-1"); // 1000.00 Kč
        vm.stopPrank();
    }

    function _paymentInfo(uint120 maxAmount) internal view returns (PaymentInfo memory) {
        return PaymentInfo({
            operator: operator,
            payer: buyer,
            receiver: seller,
            token: address(token),
            maxAmount: maxAmount,
            preApprovalExpiry: uint48(block.timestamp + 10 minutes),
            authorizationExpiry: uint48(block.timestamp + 24 hours),
            refundExpiry: uint48(block.timestamp + 7 days),
            minFeeBps: 0,
            maxFeeBps: 0,
            feeReceiver: address(0),
            salt: uint256(keccak256("deal-1:attempt-1"))
        });
    }

    /// Buyer signs ReceiveWithAuthorization(to = collector, value = maxAmount, nonce = getHash(pi with payer 0)).
    function _authorize(PaymentInfo memory pi) internal {
        PaymentInfo memory noPayer = pi;
        noPayer.payer = address(0);
        bytes32 nonce = ESCROW.getHash(noPayer);
        pi.payer = buyer;
        bytes32 structHash =
            keccak256(abi.encode(RECEIVE_TYPEHASH, buyer, COLLECTOR, uint256(pi.maxAmount), 0, pi.preApprovalExpiry, nonce));
        (uint8 v, bytes32 r, bytes32 s) =
            vm.sign(buyerKey, keccak256(abi.encodePacked("\x19\x01", token.DOMAIN_SEPARATOR(), structHash)));
        vm.prank(operator);
        ESCROW.authorize(pi, pi.maxAmount, COLLECTOR, abi.encodePacked(r, s, v));
    }

    function test_authorize_capture_void() public {
        bytes32 router = token.ROUTER_ROLE();
        vm.prank(admin);
        token.grantRole(router, store);
        PaymentInfo memory pi = _paymentInfo(20_000); // 200.00 Kč locked
        _authorize(pi);
        bytes32 h = ESCROW.getHash(pi);
        (, uint120 capturable,) = ESCROW.paymentState(h);
        assertEq(capturable, 20_000);
        assertEq(token.balanceOf(buyer), 80_000);
        assertEq(token.balanceOf(store), 20_000);

        vm.prank(operator);
        ESCROW.capture(pi, 15_000, 0, address(0)); // 150.00 Kč accepted
        assertEq(token.balanceOf(seller), 15_000);

        vm.prank(operator);
        ESCROW.void(pi); // the rest back to the buyer
        (bool collected, uint120 left, uint120 refundable) = ESCROW.paymentState(h);
        assertTrue(collected);
        assertEq(left, 0);
        assertEq(refundable, 15_000);
        assertEq(token.balanceOf(buyer), 85_000);
        assertEq(token.balanceOf(store), 0);
        assertEq(token.totalSupply(), 100_000);
    }

    /// Why ROUTER on the token store matters: without it the locked money cannot move at all.
    function test_store_without_router_cannot_release() public {
        PaymentInfo memory pi = _paymentInfo(5_000);
        _authorize(pi); // collector holds ROUTER, so locking works
        vm.prank(operator);
        vm.expectRevert(); // TransferViaBankOnly(store), wrapped by the escrow's SafeERC20
        ESCROW.capture(pi, 5_000, 0, address(0));
        bytes32 router = token.ROUTER_ROLE();
        vm.prank(admin);
        token.grantRole(router, store);
        vm.prank(operator);
        ESCROW.capture(pi, 5_000, 0, address(0));
        assertEq(token.balanceOf(seller), 5_000);
    }

    /// The bank decides who may be paid: a receiver off the allowlist makes capture revert, money stays locked.
    function test_capture_to_delisted_receiver_reverts() public {
        bytes32 router = token.ROUTER_ROLE();
        vm.prank(admin);
        token.grantRole(router, store);
        PaymentInfo memory pi = _paymentInfo(5_000);
        _authorize(pi);
        vm.prank(admin);
        token.disallow(seller);
        vm.prank(operator);
        vm.expectRevert();
        ESCROW.capture(pi, 5_000, 0, address(0));
        vm.prank(operator);
        ESCROW.void(pi);
        assertEq(token.balanceOf(buyer), 100_000);
    }
}
