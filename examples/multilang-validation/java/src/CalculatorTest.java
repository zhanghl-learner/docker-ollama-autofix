public final class CalculatorTest {
    public static void main(String[] args) {
        int actual = Calculator.add(2, 3);
        if (actual != 5) {
            throw new AssertionError("Expected add(2, 3) to equal 5, received " + actual);
        }
        System.out.println("Java test passed");
    }
}
